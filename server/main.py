"""
server/main.py
==============
服务器端编排入口。

启动顺序：
  1. 加载配置 / 初始化日志 / 初始化 SQLite
  2. 启动 Notifier 的对外 WS 服务（供 Windows 客户端连接）
  3. 启动市场发现循环（Gamma REST）
  4. 启动 Polymarket 实时 WS 订阅（按 token_id）
  5. 启动短窗口巡检（识别拆单）
  6. （降级）若 WS 长时间无数据，自动用 REST /trades 轮询补全

运行：
  python main.py                 # 默认读 ./config.yaml
  python main.py --config path   # 指定配置
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

import websockets
from websockets.legacy.server import serve

# 允许 `python main.py` 直接运行（不安装为包时）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Settings, load_settings
from db import AlertDB
from detector import Detector
from models import Alert, Market, Outcome
from notifier import Notifier
from polymarket_client import (PolymarketREST, PolymarketWS, WSConfig,
                               normalize_event_to_markets)

logger = logging.getLogger("polyalert")


# ---------------------------------------------------------------------------
# 监测引擎：把数据源、检测器、通知器连起来
# ---------------------------------------------------------------------------

class MonitoringEngine:
    def __init__(self, settings: Settings, db: AlertDB, notifier: Notifier):
        self.s = settings
        self.db = db
        self.notifier = notifier
        self.detector = Detector(settings)
        self.rest = PolymarketREST(settings)

        # condition_id -> Market
        self.markets: Dict[str, Market] = {}
        # token_id -> (condition_id, outcome_name)
        self.token_index: Dict[str, tuple] = {}

        self.ws: Optional[PolymarketWS] = None
        self._stop = asyncio.Event()
        self._rediscover_now = asyncio.Event()
        self._last_ws_msg_ts = 0.0

    # ----- 市场发现 --------------------------------------------------------

    async def discovery_loop(self) -> None:
        """周期性拉取 Gamma events，过滤、更新本地市场表。"""
        interval = self.s.discovery.refresh_interval_sec
        while not self._stop.is_set():
            try:
                await self._discover_once()
            except Exception as e:  # noqa: BLE001
                logger.error(f"discovery error: {e}", exc_info=True)
            try:
                await self._wait_for_next_discovery(interval)
            except asyncio.TimeoutError:
                pass

    async def _discover_once(self) -> None:
        kw = self.s.discovery.keywords + self.s.discovery.team_whitelist
        exclude = [k.lower() for k in self.s.discovery.exclude_keywords]

        events = await self.rest.discover_events(kw, limit=max(200, self.s.discovery.max_markets * 2))
        candidates: list[tuple[int, float, str, Market]] = []
        seen_conditions: set[str] = set()

        for ev in events:
            # 标题 / 描述 / slug 命中关键词
            title = (ev.get("title") or "").lower()
            slug = (ev.get("slug") or "").lower()
            desc = (ev.get("description") or "").lower()
            blob = json.dumps({"title": ev.get("title", ""), "slug": ev.get("slug", ""),
                               "desc": ev.get("description", "")}, ensure_ascii=False).lower()
            sub_blob = json.dumps(ev.get("markets", []), ensure_ascii=False).lower()

            hit = any(k.lower() in blob for k in kw)
            if not hit:
                # 再检查子 markets 的 question
                if not any(k.lower() in sub_blob for k in kw):
                    continue
            if any(x in blob for x in exclude):
                continue

            # —— 反噪声：纯球队名（Iran/USA/France/Brazil 等）会蹭到政治/选举市场。
            #    真正的世界杯市场满足以下任一强信号：
            #      a) slug 以 fifwc- 开头（Polymarket 世界杯赛事固定前缀）
            #      b) 标题/描述含 "world cup" / "golden boot" / "fifa"
            #      c) 标题含 " vs "（对阵型市场，如 France vs. Senegal）
            #    若三者都不满足，但只命中了"通用球队名"，则丢弃。
            event_text = f"{blob} {sub_blob}"
            if not _is_world_cup_signal(slug, event_text):
                # 仅靠通用球队名命中 → 视为噪声，丢弃
                continue

            markets = normalize_event_to_markets(ev)
            for m in markets:
                if m.closed or not m.active:
                    continue
                if m.condition_id in seen_conditions:
                    continue
                if m.liquidity_usd < self.s.discovery.min_liquidity_usd:
                    continue
                market_text = f"{event_text} {m.question} {m.event_slug} {m.url}".lower()
                if not _is_world_cup_signal(slug, market_text):
                    continue
                seen_conditions.add(m.condition_id)
                candidates.append((
                    _market_priority(slug, market_text),
                    -m.liquidity_usd,
                    m.question,
                    m,
                ))

        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        selected = candidates[:self.s.discovery.max_markets]

        new_tokens: List[str] = []
        for _, _, _, m in selected:
            self.markets[m.condition_id] = m
            for o in m.outcomes:
                self.token_index[o.clob_token_id] = (m.condition_id, o.name)
                new_tokens.append(o.clob_token_id)

        cats = Counter(_market_category(m.event_slug, m.question) for _, _, _, m in selected)

        logger.info(f"discovery: {len(self.markets)} markets, {len(self.token_index)} tokens "
                    f"categories={dict(cats)}")

        # 更新 WS 订阅
        if new_tokens and self.ws:
            self.ws.update_assets(new_tokens)
            # 让主循环感知“有数据”
            self._last_ws_msg_ts = time.time()

        # 初始化盘口深度（一次性补全，后续靠 WS 推送）
        await self._refresh_books_and_prices()

    async def _refresh_books_and_prices(self) -> None:
        """拉一次盘口 + 价格，给 detector 提供初始深度。"""
        tokens = list(self.token_index.keys())
        for i in range(0, len(tokens), 25):
            batch = tokens[i:i + 25]
            for tid in batch:
                cond, _ = self.token_index.get(tid, ("", ""))
                m = self.markets.get(cond)
                if not m:
                    continue
                o = m.find_outcome(tid)
                if not o:
                    continue
                try:
                    book = await self.rest.get_orderbook(tid)
                    book_price = _price_from_book(book)
                    if book_price > 0:
                        o.price = book_price
                        await self.detector.on_price(tid, o.price)
                    depth = book.get("depth_bid_usd", 0) + book.get("depth_ask_usd", 0)
                    await self.detector.on_book(tid, depth)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"book {tid}: {e}")
            await asyncio.sleep(0.2)  # 限速缓冲

    # ----- 实时 WS ---------------------------------------------------------

    async def ws_loop(self) -> None:
        """维护 Polymarket 实时 WS 连接，订阅当前所有 token。"""
        while not self._stop.is_set():
            tokens = list(self.token_index.keys())
            if not tokens:
                # 没有市场，等待发现
                await asyncio.sleep(10)
                continue

            cfg = WSConfig(url=self.s.polymarket.ws_market_url,
                           channel="market", assets_ids=tokens, name="market")
            self.ws = PolymarketWS(cfg, on_message=self._on_ws_msg,
                                   on_status=self._on_ws_status)
            await self.ws.start()
            # 等待停止或市场刷新（每轮重新订阅）
            while not self._stop.is_set():
                await asyncio.sleep(15)
                # 若订阅集合变化较大，重启 WS 以应用新订阅
                if set(self.token_index.keys()) - set(cfg.assets_ids):
                    break
            await self.ws.stop()

    async def _on_ws_status(self, status: str) -> None:
        logger.info(f"[ws-status] {status}")

    async def _on_ws_msg(self, msg: dict) -> None:
        self._last_ws_msg_ts = time.time()
        etype = msg.get("event_type") or msg.get("type") or ""

        # 价格变化
        if etype in ("price_change", "book", "tick_size_change"):
            asset = msg.get("asset_id") or ""
            price = msg.get("price")
            changes = msg.get("changes") or []
            # book / price_change 可能内嵌 changes 列表
            if changes and price is None:
                for ch in changes:
                    a = ch.get("asset_id") or asset
                    p = ch.get("price")
                    if a and p is not None:
                        await self._apply_price(a, float(p))
            elif asset and price is not None:
                await self._apply_price(asset, float(price))

        # 成交（last_trade_price 通道：部分场景含 size）
        elif etype in ("last_trade_price", "trade"):
            asset = msg.get("asset_id") or msg.get("market") or ""
            price = msg.get("price")
            size = msg.get("size") or msg.get("amount")
            ts_s = _to_epoch(msg.get("timestamp") or msg.get("ts"))
            if asset and price is not None:
                await self._apply_price(asset, float(price))
                if size is not None:
                    await self._handle_trade(asset, float(size), float(price), ts_s,
                                             size_missing=False)

    async def _apply_price(self, token_id: str, price: float) -> None:
        await self.detector.on_price(token_id, price)
        cond, _ = self.token_index.get(token_id, ("", ""))
        m = self.markets.get(cond)
        if m:
            o = m.find_outcome(token_id)
            if o:
                o.price = price

    async def _handle_trade(self, token_id: str, size: float, price: float,
                            ts: Optional[float], size_missing: bool) -> None:
        cond, _ = self.token_index.get(token_id, ("", ""))
        m = self.markets.get(cond)
        if not m:
            return
        o = m.find_outcome(token_id)
        if not o:
            return
        estimated_usd = size * price
        alert = await self.detector.evaluate_with_market(
            m, o, estimated_usd, size_missing=size_missing,
            window_mode=False, ts=ts)
        if alert:
            await self._emit(alert)

    async def _emit(self, alert: Alert) -> None:
        logger.warning(f"ALERT [{alert.level}] {alert.outcome_name} "
                       f"${alert.estimated_usd:,.0f} score={alert.underdog_score} "
                       f"| {alert.market_question}")
        await self.notifier.emit(alert)

    # ----- 短窗口巡检（拆单识别） -------------------------------------------

    async def window_sweep_loop(self) -> None:
        """每 20s 巡检所有 outcome 的短窗口累计成交。"""
        while not self._stop.is_set():
            try:
                for token_id, (cond, _) in list(self.token_index.items()):
                    m = self.markets.get(cond)
                    if not m:
                        continue
                    o = m.find_outcome(token_id)
                    if not o:
                        continue
                    alert = await self.detector.evaluate_window(token_id)
                    if alert:
                        # evaluate_window 不带 market 上下文，这里补全
                        full = await self.detector.evaluate_with_market(
                            m, o,
                            estimated_usd=alert.estimated_usd,
                            size_missing=False, window_mode=True)
                        if full:
                            await self._emit(full)
            except Exception as e:  # noqa: BLE001
                logger.error(f"window sweep error: {e}", exc_info=True)
            await asyncio.sleep(20)

    # ----- REST 降级补全 ---------------------------------------------------

    async def fallback_poll_loop(self) -> None:
        """WS 长时间无数据时，用 REST /trades 轮询补全成交。"""
        while not self._stop.is_set():
            await asyncio.sleep(60)
            if self._last_ws_msg_ts and (time.time() - self._last_ws_msg_ts) < 90:
                continue  # WS 正常
            logger.warning("WS 静默 >90s，启用 REST /trades 降级轮询")
            for cond, m in list(self.markets.items()):
                for o in m.outcomes:
                    try:
                        trades = await self.rest.poll_recent_trades(o.clob_token_id, limit=20)
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"fallback trades {o.clob_token_id}: {e}")
                        continue
                    for t in trades:
                        try:
                            size = float(t.get("size", 0))
                            price = float(t.get("price", 0))
                            ts_s = _to_epoch(t.get("timestamp") or t.get("createdAt"))
                        except (TypeError, ValueError):
                            continue
                        if size > 0 and price > 0:
                            await self._handle_trade(o.clob_token_id, size, price, ts_s,
                                                     size_missing=False)

    # ----- 生命周期 --------------------------------------------------------

    async def run(self) -> None:
        logger.info("MonitoringEngine starting")
        tasks = [
            asyncio.create_task(self.discovery_loop()),
            asyncio.create_task(self.ws_loop()),
            asyncio.create_task(self.window_sweep_loop()),
            asyncio.create_task(self.fallback_poll_loop()),
        ]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.rest.aclose()

    def stop(self) -> None:
        self._stop.set()

    def request_discovery_refresh(self) -> None:
        self._rediscover_now.set()

    async def _wait_for_next_discovery(self, timeout: int) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        refresh_task = asyncio.create_task(self._rediscover_now.wait())
        done, pending = await asyncio.wait(
            {stop_task, refresh_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if refresh_task in done:
            self._rediscover_now.clear()


def _to_epoch(v) -> Optional[float]:
    """把 Polymarket 的时间戳（秒 / 毫秒 / ISO 字符串）统一成 epoch 秒。"""
    if v is None:
        return None
    try:
        f = float(v)
        return f / 1000.0 if f > 1e12 else f
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _is_world_cup_signal(slug: str, text: str) -> bool:
    """Return True for explicit World Cup or FIFA match-market signals."""
    hay = f"{slug} {text}".lower()
    return (
        slug.startswith("fifwc-")
        or "world cup" in hay
        or "fifa" in hay
        or "golden boot" in hay
        or "golden ball" in hay
    )


def _market_category(slug: str, text: str) -> str:
    hay = f"{slug} {text}".lower()
    if "group" in hay:
        return "group"
    if "advance to the knockout" in hay or "knockout stages" in hay:
        return "advance"
    if ("golden boot" in hay or "golden-boot" in hay
            or "golden ball" in hay or "golden-ball" in hay
            or "top goalscorer" in hay):
        return "golden"
    if "world cup winner" in hay or "win the 2026 fifa world cup" in hay:
        return "winner"
    if slug.startswith("fifwc-"):
        return "match"
    return "other"


def _market_priority(slug: str, text: str) -> int:
    """Prefer group/advance markets before high-volume match prop markets."""
    category = _market_category(slug, text)
    order = {
        "group": 0,
        "advance": 1,
        "winner": 2,
        "golden": 3,
        "match": 4,
        "other": 8,
    }
    return order.get(category, 8)


def _price_from_book(book: dict) -> float:
    """Derive a usable implied probability from a normalized orderbook."""
    try:
        mid = float(book.get("mid_price") or 0.0)
        if mid > 0:
            return mid
        best_bid = float(book.get("best_bid") or 0.0)
        best_ask = float(book.get("best_ask") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if best_bid > 0 and 0 < best_ask < 1:
        return (best_bid + best_ask) / 2
    if best_bid > 0:
        return best_bid
    if 0 < best_ask < 1:
        return best_ask
    return 0.0


def _build_ssl_context(settings: Settings):
    """
    若配置了 ssl_cert + ssl_key，返回 SSLContext 启用 wss://。
    否则返回 None（明文 ws）。
    """
    cert = settings.server.ssl_cert.strip()
    key = settings.server.ssl_key.strip()
    if not cert or not key:
        return None
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(certfile=cert, keyfile=key)
    except Exception as e:
        logger.error(f"加载 TLS 证书失败：{e}。请检查 ssl_cert/ssl_key 路径与权限。"
                     f"将回退到明文 ws://。")
        return None
    # 推荐的安全设置
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

async def amain(settings: Settings, config_path: str | None = None) -> None:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
    db = AlertDB(settings.db_path)
    notifier = Notifier(settings, db, config_path=config_path)
    engine = MonitoringEngine(settings, db, notifier)
    notifier.on_settings_updated = engine.request_discovery_refresh

    # 构建 SSL context（若配置了证书则启用 wss://）
    ssl_ctx = _build_ssl_context(settings)

    # 对外 WS 服务（Windows 客户端连接入口）
    ws_server = await serve(
        notifier.client_handler,
        settings.server.host, settings.server.port,
        ping_interval=settings.server.ping_interval_sec, ping_timeout=30,
        max_size=2 ** 22,
        ssl=ssl_ctx,
    )
    scheme = "wss" if ssl_ctx else "ws"
    logger.info(f"client WS server on {scheme}://{settings.server.host}:{settings.server.port}"
                + (" (TLS enabled)" if ssl_ctx else " (明文 ws，建议公网部署启用 TLS)"))

    loop = asyncio.get_running_loop()
    stop_ev = asyncio.Event()

    def _on_signal(*_):
        stop_ev.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _on_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_signal)
    except NotImplementedError:
        # Windows 不支持 add_signal_handler；用 KeyboardInterrupt 兜底
        pass

    engine_task = asyncio.create_task(engine.run())
    await stop_ev.wait()
    logger.info("shutting down...")
    engine.stop()
    ws_server.close()
    await ws_server.wait_closed()
    engine_task.cancel()
    await asyncio.gather(engine_task, return_exceptions=True)


def setup_logging(settings: Settings) -> None:
    import logging.handlers  # noqa: F401  (for RotatingFileHandler)
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    root.addHandler(sh)

    fh = logging.handlers.RotatingFileHandler(
        Path(settings.log_dir) / "server.log",
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)


def main() -> None:
    import logging.handlers  # noqa: F401  (for RotatingFileHandler)
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    config_path = args.config or os.environ.get("POLYALERT_CONFIG", "config.yaml")
    settings = load_settings(config_path)
    setup_logging(settings)
    logger.info(f"config loaded; large_trade_usd={settings.detector.large_trade_usd} "
                f"underdog_threshold={settings.detector.underdog_score_threshold}")

    try:
        asyncio.run(amain(settings, config_path=config_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
