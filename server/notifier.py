"""
server/notifier.py
==================
通知分发器。

职责：
1. 维护"Windows 客户端 WS 连接池"，把 Alert 广播给所有在线客户端。
2. 提供对外 WS 服务（带 token 鉴权 + 心跳 + 断线自动重连由客户端负责）。
3. 可选二级通知：Telegram / 邮件（默认关闭）。

安全设计（已实现）：
- 鉴权优先从 HTTP 请求头读取 token，避免 URL query 明文泄露。
  - 优先头：`Authorization: Bearer <token>`
  - 次选头：`X-Auth-Token: <token>`
  - 兜底（向后兼容）：URL query `?token=`（不推荐，会在日志中泄露）
- token 比较使用 hmac.compare_digest（常量时间，抗时序攻击）。
- 单 IP 并发连接数上限（抗 DoS）。
- 支持 TLS（wss://）：配置 ssl_cert / ssl_key 后自动启用。

注意：本系统只做行情监测与提醒，绝不下单。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from ssl import create_default_context
from typing import Set

import httpx
import websockets
from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import WebSocketServerProtocol

from config import (Settings, apply_editable_settings_update,
                    editable_settings_payload, editable_settings_schema)
from db import AlertDB
from models import Alert, AlertLevel

logger = logging.getLogger("polyalert.notifier")

# 单 IP 最大并发客户端连接数（抗握手阶段 DoS）
MAX_CLIENTS_PER_IP = 8


class ClientHub:
    """管理所有已连接的 Windows 客户端 WS。"""

    def __init__(self):
        self._clients: Set[WebSocketServerProtocol] = set()
        # 按 IP 计数，用于并发连接数限制
        self._per_ip_count: dict[str, int] = {}

    def _client_ip(self, ws: WebSocketServerProtocol) -> str:
        try:
            # websockets legacy: ws.remote_address = (host, port)
            return ws.remote_address[0] if ws.remote_address else "unknown"
        except Exception:
            return "unknown"

    def add(self, ws: WebSocketServerProtocol) -> bool:
        """加入连接池。超过单 IP 上限则拒绝（返回 False）。"""
        ip = self._client_ip(ws)
        if self._per_ip_count.get(ip, 0) >= MAX_CLIENTS_PER_IP:
            return False
        self._clients.add(ws)
        self._per_ip_count[ip] = self._per_ip_count.get(ip, 0) + 1
        return True

    def discard(self, ws: WebSocketServerProtocol) -> None:
        self._clients.discard(ws)
        ip = self._client_ip(ws)
        if ip in self._per_ip_count:
            self._per_ip_count[ip] -= 1
            if self._per_ip_count[ip] <= 0:
                del self._per_ip_count[ip]

    async def broadcast(self, message: str) -> None:
        # 复制一份，避免遍历时被修改
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.discard(ws)


class Notifier:
    """通知总入口。"""

    def __init__(self, settings: Settings, db: AlertDB, config_path: str | None = None):
        self.s = settings
        self.db = db
        self.config_path = config_path
        self.hub = ClientHub()
        self.on_settings_updated = None

    async def emit(self, alert: Alert) -> None:
        """统一入口：写库 + 广播客户端 + 二级通知。"""
        # 1. 持久化
        try:
            await self.db.insert(alert)
        except Exception as e:  # noqa: BLE001
            # 写库失败不应阻断推送
            logger.warning(f"db insert failed: {e}")

        payload = json.dumps({"type": "alert", "data": alert.to_dict()},
                             ensure_ascii=False)

        # 2. 广播给 Windows 客户端
        try:
            await self.hub.broadcast(payload)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"broadcast failed: {e}")

        # 3. 二级通知（异步、不阻塞主流程）
        asyncio.create_task(self._secondary(alert))

    # ----- 对外 WS 服务的 handler ------------------------------------------

    async def client_handler(self, ws: WebSocketServerProtocol) -> None:
        """
        每个 Windows 客户端连接进入这里。

        鉴权流程（按优先级）：
          1. Authorization: Bearer <token>
          2. X-Auth-Token: <token>
          3. URL query ?token=xxx（向后兼容，不推荐，会在日志中泄露）

        token 比较使用 hmac.compare_digest（常量时间，抗时序攻击）。
        鉴权失败或单 IP 连接数超限均直接关闭连接。
        """
        # --- 鉴权 ---
        token = self._extract_token(ws)
        ok = (len(token) == len(self.s.server.auth_token)
              and hmac.compare_digest(token, self.s.server.auth_token))
        if not ok:
            # 不泄露具体原因，统一返回 unauthorized
            client_ip = self.hub._client_ip(ws)
            logger.warning(f"auth failed from {client_ip}")
            await ws.close(code=4401, reason="unauthorized")
            return

        # --- 连接数限制（抗 DoS）---
        if not self.hub.add(ws):
            client_ip = self.hub._client_ip(ws)
            logger.warning(f"too many connections from {client_ip}, rejecting")
            await ws.close(code=4429, reason="too many connections")
            return

        logger.info(f"client connected: {self.hub._client_ip(ws)}")
        try:
            # 服务端主动 ping 由 websockets.serve 的 ping_interval 处理；
            # 这里阻塞读取，接收客户端可能发来的查询（如 {"type":"recent"}）。
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "recent":
                    # 限制 limit 范围，防止恶意客户端请求超大结果集
                    try:
                        limit = max(1, min(int(msg.get("limit", 20)), 200))
                    except (TypeError, ValueError):
                        limit = 20
                    rows = await self.db.recent(limit=limit)
                    await ws.send(json.dumps({"type": "recent", "data": rows},
                                             ensure_ascii=False))
                elif mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif mtype == "get_settings":
                    await ws.send(json.dumps({
                        "type": "settings",
                        "data": editable_settings_payload(self.s),
                        "schema": editable_settings_schema(),
                    }, ensure_ascii=False))
                elif mtype == "update_settings":
                    try:
                        updated = apply_editable_settings_update(
                            self.s,
                            msg.get("settings") or msg.get("data") or {},
                            self.config_path,
                        )
                        if callable(self.on_settings_updated):
                            self.on_settings_updated()
                        await ws.send(json.dumps({
                            "type": "settings_ack",
                            "ok": True,
                            "data": updated,
                            "schema": editable_settings_schema(),
                        }, ensure_ascii=False))
                        await self.hub.broadcast(json.dumps({
                            "type": "settings_updated",
                            "data": updated,
                            "schema": editable_settings_schema(),
                        }, ensure_ascii=False))
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"settings update failed: {e}")
                        await ws.send(json.dumps({
                            "type": "settings_error",
                            "ok": False,
                            "error": str(e),
                        }, ensure_ascii=False))
                elif mtype == "test_alert":
                    alert = self._make_test_alert(msg)
                    await self.emit(alert)
                    await ws.send(json.dumps({"type": "test_ack", "ok": True},
                                             ensure_ascii=False))
                # 其它类型一律忽略（白名单）
        except ConnectionClosed:
            pass
        finally:
            self.hub.discard(ws)

    @staticmethod
    def _make_test_alert(msg: dict) -> Alert:
        """生成一条人工测试提醒，用于验证客户端推送链路。"""
        note = str(msg.get("note") or "服务器测试推送").strip()
        return Alert(
            timestamp=datetime.now(timezone.utc).isoformat(),
            match_name="system-test",
            market_question=f"[测试] {note}",
            outcome_name="TEST",
            current_price=0.123,
            implied_prob=0.123,
            estimated_usd=12345.0,
            underdog_score=88.8,
            reason="这是一条人工触发的测试消息，用于验证 WebSocket 推送、桌面提醒、声音和历史列表。",
            price_change_short=0.0,
            volume_change_short=0.0,
            market_url="",
            level=AlertLevel.IMPORTANT,
            disclaimer="测试消息，不代表真实行情，不构成任何投注或投资建议。",
        )

    @staticmethod
    def _extract_token(ws: WebSocketServerProtocol) -> str:
        """
        优先从 HTTP 请求头读取 token，避免 URL query 明文泄露。
        向后兼容旧的 ?token= 方式（仅在未提供 header 时使用）。
        """
        headers = {}
        try:
            # websockets legacy: request_headers 是大小写不敏感的 dict-like
            req = getattr(ws, "request_headers", None)
            if req is None and hasattr(ws, "request"):
                req = getattr(ws.request, "headers", None)
            if req is not None:
                # 转成普通 dict（key 小写）
                for k, v in req.items():
                    headers[k.lower()] = v
        except Exception:
            headers = {}

        # 1. Authorization: Bearer <token>
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and len(auth) > 7:
            return auth[7:].strip()

        # 2. X-Auth-Token: <token>
        xtok = headers.get("x-auth-token")
        if xtok:
            return xtok.strip()

        # 3. 兜底：URL query ?token=（向后兼容，不推荐）
        path = ""
        try:
            if hasattr(ws, "request"):
                path = ws.request.path
            else:
                path = getattr(ws, "path", "") or ""
        except Exception:
            path = ""
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(path).query)
        vals = q.get("token") or []
        return vals[0] if vals else ""

    # ----- 二级通知 --------------------------------------------------------

    async def _secondary(self, alert: Alert) -> None:
        try:
            if self.s.secondary.enable_telegram:
                await self._telegram(alert)
            if self.s.secondary.enable_email:
                await self._email(alert)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"secondary failed: {e}")

    async def _telegram(self, alert: Alert) -> None:
        cfg = self.s.secondary
        text = (f"*[{alert.level}]* {alert.outcome_name}\n"
                f"{alert.market_question}\n"
                f"价格 {alert.current_price} | 估算 ${alert.estimated_usd:,.0f} | "
                f"冷门分 {alert.underdog_score}\n{alert.market_url}")
        url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={
                "chat_id": cfg.telegram_chat_id,
                "text": text, "parse_mode": "Markdown",
            })

    async def _email(self, alert: Alert) -> None:
        """SMTP 发送邮件。在默认线程池执行以避免阻塞事件循环。"""
        cfg = self.s.secondary
        body = alert.to_dict()
        msg = MIMEText(json.dumps(body, ensure_ascii=False, indent=2), "plain", "utf-8")
        msg["Subject"] = f"[{alert.level}] 冷门大额提醒: {alert.outcome_name}"
        msg["From"] = cfg.email_from
        msg["To"] = cfg.email_to

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._smtp_send, cfg, msg.as_string())

    @staticmethod
    def _smtp_send(cfg, text: str) -> None:
        ctx = create_default_context()
        with smtplib.SMTP_SSL(cfg.email_smtp_host, cfg.email_smtp_port,
                              context=ctx, timeout=15) as s:
            s.login(cfg.email_username, cfg.email_password)
            s.sendmail(cfg.email_from, [cfg.email_to], text)
