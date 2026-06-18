"""
server/models.py
================
内部数据模型。把 Polymarket 公开接口返回的松散 JSON 归一化为强类型对象，
便于 detector、notifier、db 各模块统一处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Outcome:
    """市场的一个可下注结果。"""
    name: str
    clob_token_id: str
    price: float = 0.0          # 当前价格，近似隐含概率
    volume_24h: float = 0.0


@dataclass
class Market:
    """
    归一化后的市场对象。

    - condition_id / clob_token_ids 来自 Gamma API；
    - 价格、盘口等实时字段在监测过程中被更新。
    """
    condition_id: str
    question: str                       # 市场问题，例如 "Will Brazil win the 2026 World Cup?"
    event_slug: str = ""                # 所属 event 的 slug，用于拼链接
    outcomes: List[Outcome] = field(default_factory=list)
    liquidity_usd: float = 0.0
    volume_total: float = 0.0
    active: bool = True
    closed: bool = False
    end_date: Optional[str] = None      # ISO 字符串
    url: str = ""

    # 运行期统计（短窗口）—— 由 detector 维护
    recent_trades: List[dict] = field(default_factory=list)

    def find_outcome(self, token_id: str) -> Optional[Outcome]:
        for o in self.outcomes:
            if o.clob_token_id == token_id:
                return o
        return None

    @property
    def underdog_outcome(self) -> Optional[Outcome]:
        """返回当前价格最低的 outcome（最冷门方向）。"""
        if not self.outcomes:
            return None
        return min(self.outcomes, key=lambda o: o.price)

    @property
    def favorite_outcome(self) -> Optional[Outcome]:
        if not self.outcomes:
            return None
        return max(self.outcomes, key=lambda o: o.price)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AlertLevel:
    INFO = "INFO"           # 普通提醒
    IMPORTANT = "IMPORTANT" # 重要
    SEVERE = "SEVERE"       # 严重


@dataclass
class Alert:
    """一条完整提醒。会被推送至客户端、写入 SQLite。"""
    timestamp: str
    match_name: str
    market_question: str
    outcome_name: str
    current_price: float
    implied_prob: float
    estimated_usd: float
    underdog_score: float
    reason: str
    price_change_short: float
    volume_change_short: float
    market_url: str
    level: str = AlertLevel.INFO
    disclaimer: str = "本提醒仅为行情监测，不构成任何投注或投资建议。"

    def to_dict(self) -> dict:
        return asdict(self)
