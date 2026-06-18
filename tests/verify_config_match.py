"""临时验证脚本：确认补全的队伍名 + 反噪声逻辑能精准命中世界杯市场。
显式加载 config.yaml.example，并复刻 main._discover_once 的过滤逻辑。
"""
import sys, asyncio, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from config import load_settings
from polymarket_client import PolymarketREST

async def main():
    example = str(Path(__file__).resolve().parent.parent / "server" / "config.yaml.example")
    s = load_settings(example, force=True)
    print(f"keywords: {len(s.discovery.keywords)} | team_whitelist: {len(s.discovery.team_whitelist)}")

    rest = PolymarketREST(s)
    events = await rest.discover_events(s.discovery.keywords, limit=200)
    print(f"\nGamma 返回 event 总数: {len(events)}")

    kw = [k.lower() for k in s.discovery.keywords + s.discovery.team_whitelist]
    exclude = [k.lower() for k in s.discovery.exclude_keywords]

    kept, dropped_noise = [], []
    for ev in events:
        title = (ev.get("title") or "").lower()
        slug = (ev.get("slug") or "").lower()
        desc = (ev.get("description") or "").lower()
        blob = json.dumps({"title": ev.get("title", ""), "slug": ev.get("slug", ""),
                           "desc": ev.get("description", "")}, ensure_ascii=False).lower()

        hit = any(k in blob for k in kw)
        if not hit:
            sub = json.dumps(ev.get("markets", []), ensure_ascii=False).lower()
            if not any(k in sub for k in kw):
                continue
        if any(x in blob for x in exclude):
            dropped_noise.append((ev.get("title"), "exclude_keyword"))
            continue

        strong = (slug.startswith("fifwc-") or "world cup" in blob
                  or "golden boot" in blob or "fifa" in blob
                  or " vs " in title or " vs " in desc)
        if not strong:
            dropped_noise.append((ev.get("title"), "no_strong_signal"))
            continue
        kept.append(ev)

    print(f"\n✅ 最终保留（世界杯市场）: {len(kept)} 个")
    for ev in kept[:25]:
        print(f"   - {ev.get('title')}  (slug={ev.get('slug')})")
    print(f"\n❌ 被反噪声逻辑丢弃: {len(dropped_noise)} 个")
    for t, reason in dropped_noise[:15]:
        print(f"   - [{reason}] {t}")

    await rest.aclose()

asyncio.run(main())
