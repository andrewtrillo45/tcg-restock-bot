"""One scan, then exit — the GitHub Actions mode (no server needed).

    python scan_once.py

Reads config.json (who's tracking what), remembers what it already alerted in state/,
and posts to Discord through a webhook. The workflow in .github/workflows/scan.yml runs
this every 10 minutes and commits state/ back so nothing is alerted twice.

Environment: DISCORD_WEBHOOK (required), NEWS_WEBHOOK (optional, defaults to DISCORD_WEBHOOK),
BESTBUY_API_KEY (recommended), TARGET_KEY (only if Target rotates its key), FORCE_ALERT=1
to post everything currently in stock.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from checkers import (BestBuyChecker, NewsChecker, PageWatchChecker, SearchPageChecker, ShopifyChecker,
                      TargetChecker)
from engine import Engine
from geo import lookup_zip, valid_zip
from http_client import AiohttpFetcher
from render import alert_message, digest_text, drop_message, health_changes, listing_message, news_message
from state import ALL_GAMES, State

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scan_once")

CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")
STATE_DIR = os.getenv("STATE_DIR", "state")
WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
NEWS_WEBHOOK = os.getenv("NEWS_WEBHOOK", "") or WEBHOOK
GAME_WEBHOOKS = {"pokemon": os.getenv("POKEMON_WEBHOOK", ""), "yugioh": os.getenv("YUGIOH_WEBHOOK", ""),
                 "funko": os.getenv("FUNKO_WEBHOOK", "")}
ANY_GAME = GAME_WEBHOOKS["pokemon"] or GAME_WEBHOOKS["yugioh"]
STATUS_WEBHOOK = os.getenv("STATUS_WEBHOOK", "") or NEWS_WEBHOOK or GAME_WEBHOOKS["pokemon"] or ANY_GAME


def hook(game: str, kind: str) -> str:
    """Which webhook a post goes to: the game's own channel if set, else alerts/news, else any game channel."""
    if game == "funko":   # Funko never spills into the card channels
        return GAME_WEBHOOKS["funko"] or (NEWS_WEBHOOK if kind == "news" else WEBHOOK)
    return GAME_WEBHOOKS.get(game, "") or (NEWS_WEBHOOK if kind == "news" else WEBHOOK) or ANY_GAME
FORCE_ALERT = os.getenv("FORCE_ALERT", "") in ("1", "true", "yes")
# SOURCES=walmart  → run only that check (the "helper" mode for a home PC); empty = everything
SOURCES = {x.strip().lower() for x in os.getenv("SOURCES", "").split(",") if x.strip()}


class ConfigError(SystemExit):
    pass


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise ConfigError(f"{CONFIG_PATH} is missing — copy config.example.json to config.json and edit it.")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{CONFIG_PATH} isn't valid JSON: {e}. A missing comma or quote is the usual cause.")
    trackers = cfg.get("trackers") or []
    if not trackers:
        raise ConfigError("config.json has no trackers — add at least one with a zip and radius.")
    for t in trackers:
        z = str(t.get("zip", "")).strip()
        if not valid_zip(z):
            raise ConfigError(f'Tracker "{t.get("name", "?")}" needs a real 5-digit zip (got "{z}").')
    return cfg


async def build_state(cfg: dict, fetcher) -> State:
    """Turn config.json into the State object the engine expects, keeping auto-added drops."""
    os.makedirs(STATE_DIR, exist_ok=True)
    st = State(os.path.join(STATE_DIR, "runtime.json"))
    st.load_file()
    old_geo = {t["zip"]: t for t in st.trackers.values() if t.get("lat") is not None}
    old_geo.update({s["zip"]: s for s in st.shops if s.get("lat") is not None and s.get("zip")})
    auto_drops = [d for d in st.drops if d.get("source") != "manual"]
    st.trackers, st.shops, st.urls, st.skus, st.feeds, st.drops = {}, [], [], [], [], auto_drops

    async def geo(zip_code: str) -> dict | None:
        zip_code = str(zip_code).strip()[:5]
        if zip_code in old_geo:
            o = old_geo[zip_code]
            return {"zip": zip_code, "lat": o["lat"], "lon": o["lon"],
                    "city": o.get("city", ""), "state": o.get("state", "")}
        return await lookup_zip(fetcher, zip_code)

    for i, t in enumerate(cfg["trackers"]):
        info = await geo(t["zip"])
        if not info:
            log.error('zip %s for "%s" could not be looked up — skipping that tracker', t["zip"], t.get("name"))
            continue
        uid = str(t.get("discord_user_id") or "").strip() or f"cfg{i}"
        games = [g for g in (t.get("games") or ALL_GAMES) if g in ALL_GAMES] or list(ALL_GAMES)
        st.trackers[uid] = {"user_id": uid, "name": t.get("name") or f"tracker {i + 1}", "zip": info["zip"],
                            "lat": info["lat"], "lon": info["lon"], "city": info["city"], "state": info["state"],
                            "radius": int(t.get("radius") or 50), "games": games, "dm": False,
                            "online": bool(t.get("online", True)), "since": 0}
    for s in cfg.get("shops") or []:
        info = await geo(s["zip"]) if s.get("zip") else None
        st.shops.append({"key": s["name"].lower().replace(" ", "-"), "name": s["name"], "url": s["url"],
                         "all": bool(s.get("all")), **({"zip": info["zip"], "lat": info["lat"], "lon": info["lon"],
                                                        "city": info["city"], "state": info["state"]} if info else {})})
    for p in cfg.get("pages") or []:
        st.urls.append({"key": p["url"].lower().replace("/", "-")[:60], "url": p["url"],
                        "name": p.get("name", ""), "site": p["url"].split("/")[2].replace("www.", "") if "//" in p["url"] else ""})
    for k in cfg.get("pinned_skus") or []:
        st.skus.append({"retailer": k["retailer"], "id": "".join(ch for ch in str(k["id"]) if ch.isdigit()),
                        "name": k.get("name", ""), "added": 0})
    st.feeds = list(cfg.get("feeds") or [])
    for d in cfg.get("drops") or []:
        st.upsert_drop(d["name"], d["date"], d.get("game", "pokemon"), source="manual", url=d.get("url", ""))
    st.save()
    return st


def write_status_page_data(state, engine, checkers, misc) -> None:
    from geo import haversine_mi
    from render import RETAILER_LABEL, SOURCE_LABELS
    import filters
    trackers = list(state.trackers.values())
    home = trackers[0] if trackers else None
    items = []
    for h in engine.last_hits:
        p = h.product
        rec = {"game": p.game, "title": p.title, "url": p.url, "price": p.price, "image": p.image,
               "retailer": RETAILER_LABEL.get(p.retailer, p.retailer), "note": h.note, "qty": h.qty,
               "tags": filters.funko_tags(p.title) if p.game == "funko" else []}
        if h.store is not None:
            rec["store"] = h.store.label
            if home and h.store.lat is not None and home.get("lat") is not None:
                rec["miles"] = round(haversine_mi(home["lat"], home["lon"], h.store.lat, h.store.lon), 1)
        items.append(rec)
    items.sort(key=lambda r: (r["game"], r.get("miles", 9999), r["title"]))
    health = []
    for ch in list(checkers) + ([engine.news] if engine.news else []):
        st_, detail = engine.health.get(ch.name, ("idle", ""))
        health.append({"name": SOURCE_LABELS.get(ch.name, ch.label), "ok": st_ == "ok", "detail": detail})
    data = {"updated_at": time.time(), "home": (f"{home['city']}, {home['state']} {home['zip']} · {home['radius']} mi" if home else ""),
            "items": items, "drops": state.upcoming_drops(60), "health": health,
            "games": {g: filters.GAMES[g]["label"] for g in filters.GAMES}}
    os.makedirs("docs", exist_ok=True)
    with open(os.path.join("docs", "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


class Poster:
    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.sent = 0

    async def send(self, webhook: str, content: str, embed: dict | None = None) -> None:
        if not webhook:
            log.warning("no webhook set (DISCORD_WEBHOOK / POKEMON_WEBHOOK / YUGIOH_WEBHOOK) — would have posted: %s",
                        content.splitlines()[0])
            return
        body = {"content": content, "allowed_mentions": {"parse": ["users"]}}
        if embed:
            body["embeds"] = [embed]
        for attempt in range(4):
            status, data = await self.fetcher.post_json(webhook + ("&" if "?" in webhook else "?") + "wait=true", body)
            if status in (200, 204):
                self.sent += 1
                await asyncio.sleep(0.6)       # webhooks allow ~5 posts / 2 s
                return
            if status == 429:
                wait = float((data or {}).get("retry_after", 2)) + 0.5
                await asyncio.sleep(min(wait, 15))
                continue
            log.error("webhook returned HTTP %s: %s", status, str(data)[:200])
            return


async def main() -> int:
    cfg = load_config()
    settings = cfg.get("settings") or {}
    tz = ZoneInfo(settings.get("timezone") or os.getenv("TZ_NAME", "America/Denver"))
    os.environ.setdefault("TZ_NAME", str(tz))
    fetcher = AiohttpFetcher()
    await fetcher.start()
    try:
        state = await build_state(cfg, fetcher)
        def want(name: str) -> bool:
            return not SOURCES or name in SOURCES
        sp_sites = {"walmart", "gamestop", "hottopic", "fye"}
        if os.getenv("WALMART_ON_PC", "").lower() in ("1", "true", "yes") and not SOURCES:
            settings["walmart"] = False   # a home PC is doing Walmart; don't double-post from GitHub
        checkers = []
        if want("target"):
            checkers.append(TargetChecker(fetcher, key=os.getenv("TARGET_KEY") or None,
                                          max_stores=int(settings.get("max_target_stores") or 20)))
        if want("bestbuy"):
            checkers.append(BestBuyChecker(fetcher, os.getenv("BESTBUY_API_KEY", "")))
        if want("shops"):
            checkers.append(ShopifyChecker(fetcher))
        if want("urls"):
            checkers.append(PageWatchChecker(fetcher))
        if not SOURCES or SOURCES & sp_sites:
            checkers.append(SearchPageChecker(
                fetcher,
                walmart=bool(settings.get("walmart", True)) and want("walmart"),
                gamestop=bool(settings.get("gamestop", True)) and want("gamestop"),
                hottopic=bool(settings.get("hottopic", True)) and want("hottopic"),
                fye=bool(settings.get("fye", True)) and want("fye")))
        if not checkers:
            raise ConfigError(f"SOURCES={sorted(SOURCES)} matched nothing (use target, bestbuy, shops, urls, walmart, gamestop, hottopic, fye)")
        engine = Engine(state, checkers, os.path.join(STATE_DIR, "seen.json"),
                        realert_hours=float(settings.get("realert_hours") or 6),
                        news=NewsChecker(fetcher) if want("news") else None)
        first_run = not engine.baselined
        alerts = await engine.scan(force_alert=FORCE_ALERT)
        news = await engine.scan_news()
        state.prune_drops()
        state.save()

        poster = Poster(fetcher)
        for a in alerts:
            a.recipients = [(u, m) for u, m in a.recipients if u.isdigit()]   # mention only real Discord IDs
            content, embed = alert_message(a)
            await poster.send(hook(a.product.game, "alert"), content, embed)
        for prod in engine.new_listings:
            await poster.send(hook(prod.game, "news"), *listing_message(prod))
        for d in engine.new_drops:
            await poster.send(hook(d.get("game", ""), "news"), *drop_message(d))
        for n in news:
            await poster.send(hook(n.game, "news"), *news_message(n, interactive=False))

        # morning digest, once per local day
        misc_path = os.path.join(STATE_DIR, "misc.json")
        misc = {}
        if os.path.exists(misc_path):
            with open(misc_path, encoding="utf-8") as f:
                misc = json.load(f)
        now = datetime.now(tz)
        today = now.date().isoformat()
        digest_hour = settings.get("digest_hour")
        digest_hour = 9 if digest_hour in (None, "") else int(digest_hour)
        if want("news") and now.hour >= digest_hour and misc.get("last_digest") != today:
            if ANY_GAME:
                for game in ("pokemon", "yugioh", "funko"):
                    if any(d.get("game") == game for d in state.upcoming_drops(14)) and hook(game, "news"):
                        await poster.send(hook(game, "news"), digest_text(state, 14, interactive=False, game=game))
            elif state.upcoming_drops(14):
                await poster.send(NEWS_WEBHOOK, digest_text(state, 14, interactive=False))
            misc["last_digest"] = today
        # tell Discord when a source breaks or recovers (once per change, not every run)
        prev_health = dict(misc.get("health") or {})
        prev_health["searchpages_detail"] = misc.get("searchpages_detail", "")
        now_health = {k: v[0] for k, v in engine.health.items()}
        for content, embed in health_changes(prev_health, engine.health):
            await poster.send(STATUS_WEBHOOK, content, embed)
        misc["health"] = now_health
        misc["searchpages_detail"] = engine.health.get("searchpages", ("", ""))[1]
        page = os.getenv("STATUS_PAGE_URL", "").strip()
        if page and misc.get("page_announced") != page:
            for game in ("pokemon", "yugioh", "funko"):
                h = GAME_WEBHOOKS.get(game) or ""
                if h:
                    await poster.send(h, f"📊 **Live stock page** — everything in stock right now, drops coming up, "
                                         f"and which stores are working. Bookmark it:\n{page}\n"
                                         f"(updates every scan · pin this message)")
            misc["page_announced"] = page
        misc["last_run"] = time.time()
        with open(misc_path, "w", encoding="utf-8") as f:
            json.dump(misc, f)

        # live status page data (served by GitHub Pages from /docs)
        try:
            write_status_page_data(state, engine, checkers, misc)
        except Exception as e:
            log.warning("status page data failed: %s", e)

        # summary for the Actions log
        print("\n=== scan summary ===")
        for ch in checkers + ([engine.news] if engine.news else []):
            st_, detail = engine.health.get(ch.name, ("idle", ""))
            print(("OK  " if st_ == "ok" else "FAIL") + f" {ch.label}: {detail}")
        print(f"in stock now: {len(engine.last_hits)} · alerts: {len(alerts)} · new listings: {len(engine.new_listings)}"
              f" · news: {len(news)} · posted: {poster.sent}" + ("  (first run = snapshot only)" if first_run else ""))
        failed = [n for n, (s_, _) in engine.health.items() if s_ == "error"]
        if failed and len(failed) == len(engine.health):
            print("every source failed — check secrets and network")
            return 1
        return 0
    finally:
        await fetcher.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
