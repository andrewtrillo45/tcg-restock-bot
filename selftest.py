"""Check the stores from your own computer, no Discord needed.

    python selftest.py 80202            # 50-mile circle around ZIP 80202
    python selftest.py 80202 30         # 30-mile circle
    python selftest.py 80202 50 https://www.walmart.com/ip/...   # also test a product page

Prints every Pokémon / Yu-Gi-Oh! item currently in stock in that circle, plus which
retailer checks are working (✅) or failing (❌) and why.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from checkers import BestBuyChecker, NewsChecker, PageWatchChecker, ShopifyChecker, TargetChecker
from engine import Engine
from geo import lookup_zip
from http_client import AiohttpFetcher
from state import State


async def main(zip_code: str, radius: int, extra_urls: list[str]) -> None:
    fetcher = AiohttpFetcher()
    await fetcher.start()
    try:
        info = await lookup_zip(fetcher, zip_code)
        if not info:
            print(f"❌ ZIP {zip_code} not found (or api.zippopotam.us unreachable)")
            return
        print(f"📍 {info['city']}, {info['state']} {info['zip']} · {radius} mi circle\n")
        tmp = tempfile.mkdtemp()
        state = State(os.path.join(tmp, "state.json"))
        state.set_tracker("0", "selftest", info, radius, ["pokemon", "yugioh"], False, True)
        for i, u in enumerate(extra_urls):
            state.add_url(u, f"test page {i + 1}")
        checkers = [
            TargetChecker(fetcher, key=os.getenv("TARGET_KEY") or None,
                          max_stores=int(os.getenv("MAX_TARGET_STORES", "20"))),
            BestBuyChecker(fetcher, os.getenv("BESTBUY_API_KEY", "")),
            ShopifyChecker(fetcher),
            PageWatchChecker(fetcher),
        ]
        engine = Engine(state, checkers, os.path.join(tmp, "seen.json"), news=NewsChecker(fetcher))
        print("Scanning… (Target can take a minute: one call per store per 20 products)\n")
        await engine.scan()
        await engine.scan_news()
        for ch in checkers + [engine.news]:
            st, detail = engine.health.get(ch.name, ("idle", ""))
            print(("✅" if st == "ok" else "❌") + f" {ch.label}: {detail}")
            if st == "error":
                print("     ↳", detail)
        local, online = engine.stock_for(state.trackers["0"])
        print(f"\n🏬 In stock locally ({len(local)}):")
        for h, d in local:
            qty = f" · {h.qty} left" if h.qty is not None else ""
            print(f"  {d:5.1f} mi  {h.store.label:38.38}  {h.product.title[:60]}{qty}")
        print(f"\n🌐 In stock online ({len(online)}):")
        for h in online:
            print(f"  {h.note:18.18}  {h.product.title[:60]}  {h.product.url}")
        if not local and not online:
            print("  (nothing right now — if every checker shows ❌ above, see README → Troubleshooting)")
        ups = state.upcoming_drops(120)
        print(f"\n🗓️ Upcoming drops with retailer street dates ({len(ups)}):")
        for d in ups[:15]:
            print(f"  {d['date']}  {d['name'][:60]}  ({d.get('retailer') or d['source']} {d.get('sku', '')})")
        news = engine.news._cache[:8] if engine.news else []
        print(f"\n📰 Latest drop news ({len(engine.news._cache) if engine.news else 0} relevant articles):")
        for n in news:
            print(f"  {n.source:18.18}  {n.title[:70]}" + (f"  📅 {n.release_date}" if n.release_date else ""))
    finally:
        await fetcher.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    z = sys.argv[1]
    r = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 50
    urls = [a for a in sys.argv[2:] if a.startswith("http")]
    asyncio.run(main(z, r, urls))
