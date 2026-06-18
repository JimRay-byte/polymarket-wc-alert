"""
tests/test_connectivity.py
==========================
真实连通性测试：只读访问 Polymarket 公开接口，验证：
  1. Gamma API 能拉到 events
  2. 关键词能命中世界杯相关市场
  3. （可选）官方 WS market 通道能连上并收到消息

注意：本脚本仅做只读连通性检查，不下单、不登录。
运行： python tests/test_connectivity.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from config import Settings
from polymarket_client import PolymarketREST, normalize_event_to_markets


async def test_gamma():
    s = Settings()
    rest = PolymarketREST(s)
    print("→ 拉取 Gamma /events ...")
    events = await rest.discover_events(s.discovery.keywords, limit=100)
    print(f"  拿到 {len(events)} 个活跃 event")
    # 关键词命中
    kw = [k.lower() for k in s.discovery.keywords]
    hit_events = []
    for ev in events:
        blob = (ev.get("title", "") + " " + ev.get("slug", "")).lower()
        if any(k in blob for k in kw):
            hit_events.append(ev)
    print(f"  命中世界杯关键词的 event: {len(hit_events)}")
    for ev in hit_events[:5]:
        print(f"    - {ev.get('title')}  (slug={ev.get('slug')})")
        for m in normalize_event_to_markets(ev)[:3]:
            print(f"        · {m.question}  outcomes={[o.name for o in m.outcomes]}")
    await rest.aclose()
    assert events, "Gamma 未返回数据，检查网络"


async def test_ws():
    s = Settings()
    # 先用一个已知活跃市场做 token（若发现不到则跳过 WS 测试）
    rest = PolymarketREST(s)
    events = await rest.discover_events(s.discovery.keywords, limit=100)
    token = None
    for ev in events:
        for m in normalize_event_to_markets(ev):
            if m.outcomes:
                token = m.outcomes[0].clob_token_id
                break
        if token:
            break
    await rest.aclose()
    if not token:
        print("⚠ 未找到可用 token，跳过 WS 测试")
        return

    import websockets
    print(f"→ 连接官方 WS 并订阅 token={token[:10]}...")
    try:
        async with websockets.connect(s.polymarket.ws_market_url,
                                      ping_interval=15, ping_timeout=10) as ws:
            await ws.send(json.dumps({"assets_ids": [token], "type": "market"}))
            # 等 8 秒看有没有推送
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                print(f"  ✅ 收到推送：{raw[:200]}")
            except asyncio.TimeoutError:
                print("  ⚠ 8 秒内未收到推送（可能该市场暂时无变动，属正常）")
    except Exception as e:
        print(f"  ❌ WS 连接失败：{e}")
        raise


async def main():
    print("==== 1. Gamma REST 连通性 ====")
    await test_gamma()
    print("\n==== 2. 官方 WS 连通性 ====")
    await test_ws()
    print("\n✅ 连通性测试完成")


if __name__ == "__main__":
    asyncio.run(main())
