"""
server/detector.py
==================
冷门大额下注检测器。

核心产出三个量，全部透明可解释：

1. estimated_usd        —— 单笔 / 短窗口累计成交额（USDC 口径）
2. underdog_score (0-100)—— 综合冷门评分
3. alert_level          —— INFO / IMPORTANT / SEVERE

--------------------------------------------------------------------------
## 1. 成交额估算（口径与假设）

Polymarket 实时 WS 的 `last_trade_price` 不含成交数量；但 `activity` / `trades`
通道推送的 Trade 事件含 `size` 与 `price`。二元市场每份份额结算为 1 USDC，
因此：

    estimated_usd(单笔) = size × price        （price ∈ [0,1]）

若本笔成交缺少 size（部分通道只推价格），则回退：
    - 用短窗口内累积的“价格跳变 × 该市场平均单笔规模”做粗估；
    - 并在 reason 中标注 "size_missing，估算不确定"。

--------------------------------------------------------------------------
## 2. underdog_score 计算（加权求和，0-100）

五个子项，各自 0~100，最终加权：

    S = 0.30·A + 0.25·B + 0.20·C + 0.15·D + 0.10·E

  A. 隐含概率冷度
        隐含概率越低、越冷门。p <= 0.05 → 100 分；p >= 0.35 → 0 分；线性插值。
  B. 相对弱势
        该 outcome 价格相对同市场最高 outcome 的比值；比值越低越冷。
        ratio = p_underdog / p_favorite (clip [0,1])；score = (1 - ratio)·100。
  C. 短时价格异动
        短窗口内涨幅 >= price_spike_ratio 视为满分；线性映射。
  D. 成交额异常度
        当前成交额相对近 1h 中位数的倍数；>= volume_anomaly_multiplier → 满分。
  E. 流动性匹配度（反向）
        盘口深度越充足，信号越可信。深度 >= 10·trade_usd → 100；越薄越低。

最终 S 经 [0,100] 裁剪。

--------------------------------------------------------------------------
## 3. 触发条件（全部满足才提醒）

    (a) estimated_usd           >= large_trade_usd
    (b) underdog_score          >= underdog_score_threshold
    (c) outcome 隐含概率         <= underdog_max_implied_prob
    (d) 盘口深度                >= min_book_depth_usd（排除噪声）

短窗口累计模式：若 5 分钟内同 outcome 累计成交 >= short_window_large_usd，
也视为一次"大额事件"，避免被拆单绕过。

--------------------------------------------------------------------------
## 4. alert_level

    estimated_usd >= severe_trade_usd       → SEVERE
    estimated_usd >= important_trade_usd    → IMPORTANT
    else                                   → INFO
"""
from __future__ import annotations

import asyncio
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from config import Settings
from models import Alert, AlertLevel, Market, Outcome


@dataclass
class _WindowState:
    """单个 outcome 的滚动窗口状态。"""
    prices: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=2000))   # (ts, price)
    trades: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=2000))   # (ts, usd)
    last_book_depth_usd: float = 0.0
    last_price: float = 0.0
    last_update_ts: float = 0.0


class Detector:
    """
    无状态输入、有状态统计的检测器。
    main.py 收到 WS/REST 事件后调用 Detector.on_trade / on_price / on_book，
    Detector 返回可能产生的 Alert（不满足条件返回 None）。
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self.d = settings.detector
        # key = token_id
        self._state: Dict[str, _WindowState] = defaultdict(_WindowState)
        self._lock = asyncio.Lock()
        # 去重：同一 outcome 30s 内不重复提醒同级别事件
        self._last_alert: Dict[str, Tuple[float, str]] = {}

    # ---------- 状态更新入口 ------------------------------------------------

    async def on_price(self, token_id: str, price: float, ts: Optional[float] = None) -> None:
        ts = ts or time.time()
        async with self._lock:
            st = self._state[token_id]
            st.prices.append((ts, price))
            st.last_price = price
            st.last_update_ts = ts

    async def on_book(self, token_id: str, depth_usd: float) -> None:
        async with self._lock:
            self._state[token_id].last_book_depth_usd = depth_usd

    async def on_trade(self, token_id: str, size: float, price: float,
                       ts: Optional[float] = None, size_missing: bool = False) -> Optional[Alert]:
        """
        处理一笔成交，返回 Alert 或 None。
        size_missing=True 时表示 size 是回退估算（精度低）。
        """
        ts = ts or time.time()
        estimated_usd = size * price
        # 即使 size 缺失，也记录一次价格点
        async with self._lock:
            st = self._state[token_id]
            st.prices.append((ts, price))
            st.last_price = price
            st.last_update_ts = ts
            st.trades.append((ts, estimated_usd))

        return await self._evaluate(token_id, estimated_usd, price, ts,
                                    size_missing=size_missing, window_mode=False)

    async def evaluate_window(self, token_id: str, ts: Optional[float] = None) -> Optional[Alert]:
        """
        定期调用：检查短窗口累计成交是否构成大额事件（拆单识别）。
        """
        ts = ts or time.time()
        async with self._lock:
            st = self._state.get(token_id)
            if not st:
                return None
            window = self.d.short_window_sec
            cutoff = ts - window
            window_trades = [(t, u) for (t, u) in st.trades if t >= cutoff]
            if not window_trades:
                return None
            window_usd = sum(u for _, u in window_trades)
            last_price = st.last_price or 0.0
        if window_usd < self.d.short_window_large_usd:
            return None
        return await self._evaluate(token_id, window_usd, last_price, ts,
                                    size_missing=False, window_mode=True)

    # ---------- 评分核心 ----------------------------------------------------

    async def _evaluate(self, token_id: str, estimated_usd: float, price: float,
                        ts: float, size_missing: bool, window_mode: bool) -> Optional[Alert]:
        """
        综合 A/B/C/D/E 计算 underdog_score，判定是否提醒。
        注意：评分需要 market 上下文（同市场其它 outcome），由调用方通过
        evaluate_with_market 注入；这里先算自身维度，再由 main 拼接。
        本方法在缺少 market 上下文时返回 None，把判定交给 evaluate_with_market。
        """
        # 交给带 market 上下文的版本处理
        return None  # 见 evaluate_with_market

    async def evaluate_with_market(self, market: Market, outcome: Outcome,
                                   estimated_usd: float, size_missing: bool,
                                   window_mode: bool,
                                   ts: Optional[float] = None) -> Optional[Alert]:
        """
        在已知 market 上下文下完成最终判定。这是真正的“是否提醒”决策点。
        """
        ts = ts or time.time()
        d = self.d
        token_id = outcome.clob_token_id

        # --- 前置硬过滤：噪声 & 流动性 ---
        if estimated_usd < d.large_trade_usd and not window_mode:
            return None
        if estimated_usd < d.short_window_large_usd and window_mode:
            return None
        if outcome.price > d.underdog_max_implied_prob:
            return None  # 不是冷门方向

        st = self._state.get(token_id)
        if st is None:
            return None

        # 盘口深度不足 → 视为不可靠，忽略
        depth = st.last_book_depth_usd
        if depth < d.min_book_depth_usd:
            return None

        # ---------- A. 隐含概率冷度 ----------
        p = max(0.0, min(1.0, outcome.price))
        # p<=0.05 →100 ; p>=0.35 →0
        if p <= 0.05:
            A = 100.0
        elif p >= d.underdog_max_implied_prob:
            A = 0.0
        else:
            A = 100.0 * (d.underdog_max_implied_prob - p) / (d.underdog_max_implied_prob - 0.05)

        # ---------- B. 相对弱势 ----------
        fav = market.favorite_outcome
        fav_p = fav.price if fav else 1.0
        ratio = (p / fav_p) if fav_p > 0 else 1.0
        ratio = max(0.0, min(1.0, ratio))
        B = (1.0 - ratio) * 100.0

        # ---------- C. 短时价格异动 ----------
        cutoff = ts - d.short_window_sec
        old_prices = [pr for (t, pr) in st.prices if t >= cutoff]
        if len(old_prices) >= 2:
            base = old_prices[0]
            cur = old_prices[-1]
            change_ratio = (cur - base) / base if base > 0 else 0.0
            # 涨幅达到 price_spike_ratio → 100；线性映射；下跌不计入冷门异动
            C = max(0.0, min(100.0, (change_ratio / d.price_spike_ratio) * 100.0))
        else:
            C = 0.0

        # ---------- D. 成交额异常度 ----------
        # 用近 1h 成交做中位数基准
        h_cutoff = ts - 3600.0
        hist = [u for (t, u) in st.trades if h_cutoff <= t < cutoff]
        if hist:
            try:
                med = statistics.median(hist) or 1e-9
            except statistics.StatisticsError:
                med = 1e-9
            D = min(100.0, (estimated_usd / med) / d.volume_anomaly_multiplier * 100.0)
        else:
            # 近 1h 无历史样本：用绝对量兜底（>= important 阈值即给较高分）
            D = 80.0 if estimated_usd >= d.important_trade_usd else 40.0

        # ---------- E. 流动性匹配度 ----------
        # 深度 >= 10×单笔成交 → 100；线性递减
        E = min(100.0, (depth / max(estimated_usd, 1.0) / 10.0) * 100.0)

        # ---------- 加权 ----------
        score = 0.30 * A + 0.25 * B + 0.20 * C + 0.15 * D + 0.10 * E
        score = max(0.0, min(100.0, score))

        if score < d.underdog_score_threshold:
            return None

        # ---------- 去重：同 outcome 30s 内同级别不重复 ----------
        level = self._level(estimated_usd)
        key = token_id
        last = self._last_alert.get(key)
        if last and (ts - last[0]) < 30.0 and last[1] == level:
            return None
        self._last_alert[key] = (ts, level)

        # ---------- 生成 reason ----------
        reasons: List[str] = []
        reasons.append(f"隐含概率 {p:.2%} (冷度A={A:.0f})")
        reasons.append(f"相对热门比值 {ratio:.2f} (弱势B={B:.0f})")
        if C >= 50:
            reasons.append(f"短窗价格异动 (C={C:.0f})")
        if D >= 50:
            reasons.append(f"成交额异常 (D={D:.0f})")
        if E < 40:
            reasons.append(f"盘口深度偏薄 (E={E:.0f})")
        if size_missing:
            reasons.append("size_missing，成交额为估算")
        if window_mode:
            reasons.append(f"短窗累计成交触发 (≥{d.short_window_large_usd:.0f}USDC)")

        # ---------- 价格 / 成交量变化 ----------
        price_change = self._price_change(st, cutoff)
        vol_change = self._volume_change(st, cutoff)

        return Alert(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + " UTC",
            match_name=market.event_slug or market.question,
            market_question=market.question,
            outcome_name=outcome.name,
            current_price=round(p, 4),
            implied_prob=round(p, 4),
            estimated_usd=round(estimated_usd, 2),
            underdog_score=round(score, 1),
            reason="；".join(reasons),
            price_change_short=round(price_change, 4),
            volume_change_short=round(vol_change, 2),
            market_url=market.url,
            level=level,
        )

    # ---------- 小工具 ------------------------------------------------------

    def _level(self, usd: float) -> str:
        d = self.d
        if usd >= d.severe_trade_usd:
            return AlertLevel.SEVERE
        if usd >= d.important_trade_usd:
            return AlertLevel.IMPORTANT
        return AlertLevel.INFO

    @staticmethod
    def _price_change(st: _WindowState, cutoff: float) -> float:
        series = [pr for (t, pr) in st.prices if t >= cutoff]
        if len(series) < 2 or series[0] == 0:
            return 0.0
        return series[-1] - series[0]

    @staticmethod
    def _volume_change(st: _WindowState, cutoff: float) -> float:
        """返回短窗口内的累计成交额（USDC）。客户端用其直观判断量级。"""
        window = [u for (t, u) in st.trades if t >= cutoff]
        return float(sum(window))
