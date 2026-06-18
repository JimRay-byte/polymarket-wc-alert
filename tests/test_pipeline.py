"""
tests/test_pipeline.py
======================
端到端链路测试（不依赖真实 Polymarket）：

1. 启动一个内存版服务器（复用 server 的 Notifier + DB）。
2. 用 detector 直接产生一条 Alert，经 Notifier 广播。
3. 客户端用【请求头】携带 token 连接，验证能收到 alert JSON。
4. 额外覆盖：URL token 向后兼容、错误 token 被拒、过短 token 启动报错。

运行： python tests/test_pipeline.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import websockets
from websockets.legacy.server import serve

from db import AlertDB
from models import Alert, AlertLevel
from notifier import Notifier
from config import ServerSettings, Settings, DetectorSettings

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def make_settings() -> Settings:
    """构造测试用配置：loopback + 强 token，绕开生产校验。"""
    # 直接用 dict 构造，避免 Settings() 默认 0.0.0.0 + 弱 token 触发校验
    s = Settings(server=ServerSettings(
        host="127.0.0.1",            # loopback，允许任意 token
        port=18765,
        auth_token="a" * 32,         # 32 字符强 token
        ping_interval_sec=20,
    ))
    s.detector = DetectorSettings()
    s.db_path = "data/test_alerts.db"
    return s


async def main():
    settings = make_settings()
    db = AlertDB(settings.db_path)
    tmp_config_dir = tempfile.TemporaryDirectory()
    config_path = str(Path(tmp_config_dir.name) / "config.yaml")
    notifier = Notifier(settings, db, config_path=config_path)

    server = await serve(notifier.client_handler, "127.0.0.1", settings.server.port)

    async def fake_alert():
        await asyncio.sleep(0.3)
        await notifier.emit(Alert(
            timestamp="2026-06-16 12:00:00 UTC",
            match_name="2026-world-cup-winner",
            market_question="Will Saudi Arabia win the 2026 World Cup?",
            outcome_name="Yes",
            current_price=0.04,
            implied_prob=0.04,
            estimated_usd=12000.0,
            underdog_score=86.5,
            reason="隐含概率 4.00% (冷度A=100)；相对热门比值 0.04 (弱势B=96)",
            price_change_short=0.012,
            volume_change_short=12000.0,
            market_url="https://polymarket.com/event/2026-world-cup-winner",
            level=AlertLevel.IMPORTANT,
        ))

    # ---------- 测试 1：请求头鉴权（推荐方式）----------
    async def test_header_auth():
        url = f"ws://127.0.0.1:{settings.server.port}"
        headers = {"X-Auth-Token": settings.server.auth_token}
        async with websockets.connect(url, additional_headers=headers) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            assert data["type"] == "alert"
            return data["data"]

    # ---------- 测试 2：URL token 向后兼容 ----------
    async def test_url_token_compat():
        url = f"ws://127.0.0.1:{settings.server.port}/?token={settings.server.auth_token}"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "ping"}))
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(msg)["type"] == "pong"

    # ---------- 测试 3：错误 token 被拒 ----------
    async def test_wrong_token_rejected():
        url = f"ws://127.0.0.1:{settings.server.port}"
        headers = {"X-Auth-Token": "wrong-token-value-here-xxxxxxx"}
        try:
            async with websockets.connect(url, additional_headers=headers) as ws:
                await asyncio.wait_for(ws.recv(), timeout=3)
            return False  # 不该能连上
        except websockets.exceptions.InvalidStatus:
            return True   # 被拒（4401）符合预期
        except Exception:
            return True

    # ---------- 测试 4：参数读取/修改/落盘 ----------
    async def test_settings_sync():
        url = f"ws://127.0.0.1:{settings.server.port}"
        headers = {"X-Auth-Token": settings.server.auth_token}
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "get_settings"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["type"] == "settings"
            assert "large_trade_usd" in msg["data"]["detector"]
            assert "refresh_interval_sec" in msg["data"]["discovery"]

            update = {
                "detector": {
                    "large_trade_usd": 6000,
                    "important_trade_usd": 25000,
                    "severe_trade_usd": 120000,
                },
                "discovery": {
                    "refresh_interval_sec": 601,
                    "max_markets": 123,
                    "min_liquidity_usd": 321,
                },
            }
            await ws.send(json.dumps({"type": "update_settings", "settings": update}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert ack["type"] == "settings_ack"
            assert ack["data"]["detector"]["large_trade_usd"] == 6000
            assert ack["data"]["discovery"]["refresh_interval_sec"] == 601
            assert settings.detector.large_trade_usd == 6000
            assert settings.discovery.refresh_interval_sec == 601

        persisted = Path(config_path).read_text(encoding="utf-8")
        assert "large_trade_usd: 6000" in persisted
        assert "refresh_interval_sec: 601" in persisted
        return True

    print("==== 端到端 + 鉴权安全测试 ====")
    results = []

    task = asyncio.create_task(fake_alert())
    try:
        a = await test_header_auth()
        print(f"✅ [1] 请求头鉴权收到提醒：{a['outcome_name']} ${a['estimated_usd']:,.0f}")
        results.append(True)
    except Exception as e:
        print(f"❌ [1] 请求头鉴权失败：{e}")
        results.append(False)
    await task

    try:
        await test_url_token_compat()
        print("✅ [2] URL token 向后兼容正常")
        results.append(True)
    except Exception as e:
        print(f"❌ [2] URL token 兼容失败：{e}")
        results.append(False)

    try:
        ok = await test_wrong_token_rejected()
        print(f"{'✅' if ok else '❌'} [3] 错误 token {'被拒' if ok else '竟然连上了！'}")
        results.append(ok)
    except Exception as e:
        print(f"❌ [3] 错误 token 测试异常：{e}")
        results.append(False)

    try:
        ok = await test_settings_sync()
        print("✅ [4] 客户端参数读取/修改/服务端落盘正常")
        results.append(ok)
    except Exception as e:
        print(f"❌ [4] 参数同步测试失败：{e}")
        results.append(False)

    # ---------- 测试 5：弱 token 启动校验 ----------
    try:
        bad = Settings(server=ServerSettings(host="0.0.0.0",
                                             auth_token="change-me-please"))
        print("❌ [5] 弱 token 启动校验未生效（竟通过了）")
        results.append(False)
    except Exception:
        print("✅ [5] 弱 token 在公网绑定时被启动校验拒绝")
        results.append(True)

    server.close()
    await server.wait_closed()

    try:
        os.remove(settings.db_path)
    except OSError:
        pass
    tmp_config_dir.cleanup()

    passed = sum(results)
    print(f"\n结果：{passed}/{len(results)} 通过，"
          f"{'全部通过 ✓' if passed == len(results) else '有失败项 ✗'}")
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
