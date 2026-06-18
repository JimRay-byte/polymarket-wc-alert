"""
server/polymarket_client.py
===========================
Polymarket 公开数据源封装层。

设计目标
--------
1. 只使用 Polymarket **公开、只读** 的接口，不登录、不签名、不下单：
   - Gamma REST API：市场发现（events / markets）
   - CLOB REST API：盘口 / 价格 / 历史
   - 官方实时 WebSocket：价格变化、成交流
2. 把字段名、接口地址都收拢在本文件内，官方若调整字段，只改这里即可。
3. 所有网络请求都带：超时、指数退避重试、限速；WS 带心跳与自动重连。

关于“成交额估算”（重要说明，写进注释以满足可解释性要求）
------------------------------------------------------
Polymarket 官方实时 WS 的 `last_trade_price` 通道只推送价格，不含成交数量。
而 `activity` / `trades` 通道推送的 `Trade` 事件包含 `size`（成交份额数）和
`price`。Polymarket 每个市场都是二元结果市场，每份份额最终结算为 1 USDC，
因此：
    估算成交额(USDC) = size(份额) × price(单价, 0~1)
这是 Polymarket 官方文档与 py-clob-client 内部使用的口径。
当某笔成交流缺少 size 时，回退方案是：用 `/trades` REST 接口拉取近 N 条成交，
按时间戳匹配并补全；若仍无法补全，则在提醒中标注 "估算不确定"。

备用方案（仅在官方 WS 不可用时，且默认关闭）
---------------------------------------------
- 轮询 CLOB `/trades` REST 接口（限速更严格，延迟更高）。
风险：延迟大、易触发限速；不可作为首选。本模块提供 `poll_recent_trades`
方法，由 main.py 在 WS 不可用时降级调用，并在日志中明确告警。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from config import Settings
from models import Market, Outcome


# ---------------------------------------------------------------------------
# 小工具：限速器 + 指数退避重试
# ---------------------------------------------------------------------------

class RateLimiter:
    """极简令牌桶：每 `rate` 秒最多 1 次。用于 REST 接口限速。"""

    def __init__(self, rate: float = 0.2):
        self._min_interval = rate
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


async def _retry_async(coro_factory: Callable[[], Awaitable[Any]],
                       retries: int = 4, base: float = 0.8) -> Any:
    """对协程做指数退避重试。网络抖动 / 5xx 时重试，4xx 直接抛出。"""
    last_exc: Optional[Exception] = None
    for i in range(retries):
        try:
            return await coro_factory()
        except httpx.HTTPStatusError as e:
            last_exc = e
            # 4xx（除 429）一般重试无益
            if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                raise
            await asyncio.sleep(base * (2 ** i))
        except (httpx.HTTPError, OSError) as e:
            last_exc = e
            await asyncio.sleep(base * (2 ** i))
    raise RuntimeError(f"请求重试 {retries} 次仍失败: {last_exc}")


# ---------------------------------------------------------------------------
# REST 客户端：市场发现 + 行情补充
# ---------------------------------------------------------------------------

class PolymarketREST:
    """封装 Gamma / CLOB REST 接口。"""

    def __init__(self, settings: Settings):
        self.s = settings
        self.base_gamma = settings.polymarket.gamma_api_base.rstrip("/")
        self.base_clob = settings.polymarket.clob_api_base.rstrip("/")
        # 限速：保守起见，每个 host 每 0.3s 一次
        self._rl_gamma = RateLimiter(0.3)
        self._rl_clob = RateLimiter(0.3)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0),
                                         headers={"User-Agent": "polyalert/1.0 (+monitoring)"})

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----- Gamma：市场发现 -------------------------------------------------

    async def _gamma_get(self, path: str, params: Optional[dict] = None) -> Any:
        async def _do():
            await self._rl_gamma.acquire()
            r = await self._client.get(f"{self.base_gamma}{path}", params=params)
            r.raise_for_status()
            return r.json()
        return await _retry_async(_do)

    async def discover_events(self, keywords: List[str], limit: int = 200) -> List[dict]:
        """
        拉取近活跃 events，由调用方按关键词过滤。
        使用 /events?active=true&closed=false&order=volume&ascending=false
        """
        params = {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": limit,
        }
        data = await self._gamma_get("/events", params=params)
        # Gamma 可能返回 dict 或 list，统一成 list
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        return data or []

    async def search_markets(self, query: str, limit: int = 100) -> List[dict]:
        """通过 Gamma /markets 直接按关键词搜索（备用 / 辅助）。"""
        params = {"active": "true", "closed": "false",
                  "tag": query, "limit": limit}
        try:
            return await self._gamma_get("/markets", params=params) or []
        except Exception:
            # tag 过滤不命中时退回无参数
            params.pop("tag")
            return await self._gamma_get("/markets", params=params) or []

    # ----- CLOB：行情补充 --------------------------------------------------

    async def _clob_get(self, path: str, params: Optional[dict] = None) -> Any:
        async def _do():
            await self._rl_clob.acquire()
            r = await self._client.get(f"{self.base_clob}{path}", params=params)
            r.raise_for_status()
            return r.json()
        return await _retry_async(_do)

    async def get_orderbook(self, token_id: str) -> dict:
        """返回 {bids:[], asks:[], mid_price, spread, depth_bid_usd, depth_ask_usd}。"""
        raw = await self._clob_get("/book", params={"token_id": token_id})
        return self._normalize_book(raw)

    @staticmethod
    def _normalize_book(raw: dict) -> dict:
        """把盘口压成可用的统计量（深度单位 USDC）。"""
        def total(side: List[dict]) -> float:
            t = 0.0
            for lvl in side:
                p = float(lvl.get("price", 0))
                sz = float(lvl.get("size", 0))
                t += p * sz
            return t

        bids = raw.get("bids") or []
        asks = raw.get("asks") or []
        best_bid = max((float(b["price"]) for b in bids), default=0.0)
        best_ask = min((float(a["price"]) for a in asks), default=1.0)
        mid = (best_bid + best_ask) / 2 if (bids and asks) else 0.0
        return {
            "bids": bids, "asks": asks,
            "best_bid": best_bid, "best_ask": best_ask,
            "mid_price": mid,
            "spread": max(0.0, best_ask - best_bid),
            "depth_bid_usd": total(bids),
            "depth_ask_usd": total(asks),
        }

    async def get_prices(self, token_ids: List[str]) -> Dict[str, float]:
        """批量取价格。CLOB /prices 接口返回 {token_id: {price, ...}}。"""
        if not token_ids:
            return {}
        # /prices 接受 market=token_id1,token_id2 参数
        params = [("market", tid) for tid in token_ids]
        try:
            data = await self._clob_get("/prices", params=params)
        except Exception:
            return {}
        out: Dict[str, float] = {}
        # 数据结构可能是 {tid: {"price": "0.5"}} 或 [{"asset_id":..,"price":..}]
        if isinstance(data, dict):
            for tid, v in data.items():
                try:
                    out[tid] = float(v.get("price") if isinstance(v, dict) else v)
                except (TypeError, ValueError):
                    continue
        elif isinstance(data, list):
            for item in data:
                try:
                    out[item["asset_id"]] = float(item["price"])
                except (KeyError, TypeError, ValueError):
                    continue
        return out

    async def poll_recent_trades(self, market_or_token: str, limit: int = 50) -> List[dict]:
        """
        备用方案：从 CLOB /trades 拉取近期成交。
        仅在 WS 不可用或需补全 size 时使用。
        """
        try:
            data = await self._clob_get("/trades",
                                        params={"market": market_or_token, "limit": limit})
        except Exception:
            return []
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        return data or []


# ---------------------------------------------------------------------------
# 归一化：把 Gamma 的 event/market JSON 转成内部 Market
# ---------------------------------------------------------------------------

def normalize_event_to_markets(event: dict) -> List[Market]:
    """
    一个 event（如 "2026 World Cup Winner"）可能含多个子 market。
    Polymarket 的常见结构：
        event = {
            "slug": "...", "title": "...",
            "markets": [
                {"conditionId":..., "question":..., "outcomes":"[\"Yes\",\"No\"]",
                 "clobTokenIds":"[\"123\",\"456\"]", "outcomePrices":"[\"0.5\",\"0.5\"]",
                 "liquidity":"...", "volume":"...", "active":..., "closed":...,
                 "endDate":"...", "url":"..."}
            ]
        }
    """
    markets: List[Market] = []
    for m in event.get("markets", []) or []:
        try:
            cond = m.get("conditionId") or m.get("condition_id") or ""
            if not cond:
                continue
            # outcomes / token ids / prices 在 Gamma 里以 JSON 字符串形式存储
            out_names = _parse_json_str(m.get("outcomes")) or _parse_list(m.get("outcome"))
            token_ids = _parse_json_str(m.get("clobTokenIds") or m.get("clob_token_ids"))
            prices = _parse_json_str(m.get("outcomePrices") or m.get("outcome_prices")) or []

            if not token_ids or len(token_ids) < 2:
                continue

            outcomes: List[Outcome] = []
            for i, tid in enumerate(token_ids):
                name = out_names[i] if i < len(out_names) else f"Outcome{i}"
                try:
                    pr = float(prices[i]) if i < len(prices) else 0.0
                except (TypeError, ValueError):
                    pr = 0.0
                outcomes.append(Outcome(name=str(name), clob_token_id=str(tid), price=pr))

            url = m.get("url") or f"https://polymarket.com/event/{event.get('slug','')}"
            markets.append(Market(
                condition_id=str(cond),
                question=str(m.get("question") or event.get("title") or ""),
                event_slug=str(event.get("slug", "")),
                outcomes=outcomes,
                liquidity_usd=float(m.get("liquidity") or 0.0),
                volume_total=float(m.get("volume") or m.get("volumeNum") or 0.0),
                active=bool(m.get("active", True)),
                closed=bool(m.get("closed", False)),
                end_date=m.get("endDate") or m.get("endDateIso"),
                url=url,
            ))
        except Exception:
            # 单条解析失败不影响整体
            continue
    return markets


def _parse_json_str(val: Any) -> List[str]:
    """Gamma 经常把列表存成 JSON 字符串。"""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val]
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            return []
    return []


def _parse_list(val: Any) -> List[str]:
    if isinstance(val, list):
        return [str(x) for x in val]
    return []


# ---------------------------------------------------------------------------
# 实时 WebSocket 客户端：自动重连 + 心跳 + 去重
# ---------------------------------------------------------------------------

@dataclass
class WSConfig:
    url: str
    channel: str               # "market" 或 "user"（本项目仅 market）
    assets_ids: List[str]      # 要订阅的 token id 列表
    name: str = "market"


class PolymarketWS:
    """
    连接官方实时 WS，订阅一批 assets_ids。

    回调：
      on_message(msg_dict) —— 每收到一条去重后的业务消息触发。
      on_status(status_str) —— 连接状态变化（connected/reconnecting/down）。

    特性：
      - 自动重连（指数退避，封顶 60s）
      - 心跳：websockets 库自带 ping/pong；额外每 15s 发一次客户端文本心跳
      - 去重：对 (event_type, asset_id, timestamp, price, size) 做短窗口去重
    """

    def __init__(self, cfg: WSConfig,
                 on_message: Callable[[dict], Awaitable[None]],
                 on_status: Optional[Callable[[str], Awaitable[None]]] = None):
        self.cfg = cfg
        self.on_message = on_message
        self.on_status = on_status or (lambda s: asyncio.sleep(0))
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # 去重缓存：保留最近 5s 的指纹
        self._seen: deque = deque(maxlen=2000)
        self._seen_ttl = 5.0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def update_assets(self, assets_ids: List[str]) -> None:
        """运行期更新订阅列表。下次重连后生效。"""
        self.cfg.assets_ids = list(dict.fromkeys(assets_ids))  # 去重保序

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 1.0  # 成功连过则重置
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                await self.on_status(f"down:{e}")
                await asyncio.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)
            else:
                # 正常断开（服务端关闭）也走退避
                await self.on_status("reconnecting")
                await asyncio.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)

    async def _connect_once(self) -> None:
        await self.on_status("connecting")
        headers = {"User-Agent": "polyalert-ws/1.0"}
        async with websockets.connect(
            self.cfg.url, additional_headers=headers,
            ping_interval=15, ping_timeout=10,
            close_timeout=5, max_size=2**22,
        ) as ws:
            await self.on_status("connected")
            # 发送订阅消息。Polymarket market channel 格式：
            #   {"assets_ids": ["..."], "type": "market"}
            sub_msg = {"assets_ids": self.cfg.assets_ids, "type": self.cfg.channel}
            await ws.send(json.dumps(sub_msg))

            # 心跳协程：每 20s 发一条空对象，部分网关用于保活
            async def _heartbeat():
                while not self._stop.is_set():
                    await asyncio.sleep(20)
                    try:
                        await ws.send("{}")
                    except Exception:
                        return

            hb_task = asyncio.create_task(_heartbeat())
            try:
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    await self._handle_raw(raw)
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except Exception:
                    pass

    async def _handle_raw(self, raw: str | bytes) -> None:
        """解析、去重、分发。Polymarket 推送可能是数组或单对象。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return  # 忽略非 JSON（如服务端保活文本）

        msgs = data if isinstance(data, list) else [data]
        now = time.time()
        # 清理过期指纹
        while self._seen and now - self._seen[0][0] > self._seen_ttl:
            self._seen.popleft()

        for m in msgs:
            if not isinstance(m, dict):
                continue
            etype = m.get("event_type") or m.get("type") or ""
            asset = m.get("asset_id") or m.get("market") or m.get("asset") or ""
            ts = m.get("timestamp") or m.get("ts") or m.get("ev") or ""
            price = m.get("price")
            size = m.get("size")
            finger = (etype, str(asset), str(ts), str(price), str(size))
            # 去重：5s 内相同指纹视为重复
            if any(f == finger for _, f in self._seen):
                continue
            self._seen.append((now, finger))
            try:
                await self.on_message(m)
            except Exception:
                # 业务回调异常不应中断 WS
                pass
