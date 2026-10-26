"""Who is tracking what. Saved as JSON; the bot also backs it up to a Discord channel
so it survives free hosts that wipe the disk on every restart."""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlsplit

ALL_GAMES = ["pokemon", "yugioh"]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "x"


class State:
    def __init__(self, path: str):
        self.path = path
        self.trackers: dict[str, dict] = {}   # user_id → tracker
        self.shops: list[dict] = []
        self.urls: list[dict] = []
        self.skus: list[dict] = []      # pinned products: {"retailer","id","name","added"}
        self.drops: list[dict] = []     # upcoming releases: {"key","name","game","date","source","url","retailer","sku"}
        self.feeds: list[str] = []      # extra RSS feeds (defaults are added in code)
        self.updated_at: float = 0

    # ---------- persistence ----------
    def to_json(self) -> str:
        return json.dumps({"version": 1, "updated_at": self.updated_at,
                           "trackers": self.trackers, "shops": self.shops, "urls": self.urls,
                           "skus": self.skus, "drops": self.drops, "feeds": self.feeds},
                          indent=2, ensure_ascii=False)

    def load_json(self, text: str) -> None:
        d = json.loads(text)
        self.trackers = {str(k): v for k, v in (d.get("trackers") or {}).items()}
        self.shops = d.get("shops") or []
        self.urls = d.get("urls") or []
        self.skus = d.get("skus") or []
        self.drops = d.get("drops") or []
        self.feeds = d.get("feeds") or []
        self.updated_at = float(d.get("updated_at") or 0)

    def load_file(self) -> bool:
        if not os.path.exists(self.path):
            return False
        with open(self.path, encoding="utf-8") as f:
            self.load_json(f.read())
        return True

    def save(self) -> None:
        self.updated_at = time.time()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(self.to_json())
        os.replace(tmp, self.path)

    # ---------- trackers ----------
    def set_tracker(self, user_id: str, name: str, zip_info: dict, radius: int,
                    games: list[str], dm: bool, online: bool) -> dict:
        t = {"user_id": str(user_id), "name": name, "zip": zip_info["zip"],
             "lat": zip_info["lat"], "lon": zip_info["lon"], "city": zip_info.get("city", ""),
             "state": zip_info.get("state", ""), "radius": int(radius),
             "games": games or list(ALL_GAMES), "dm": bool(dm), "online": bool(online),
             "since": time.time()}
        self.trackers[str(user_id)] = t
        self.save()
        return t

    def remove_tracker(self, user_id: str) -> bool:
        gone = self.trackers.pop(str(user_id), None) is not None
        if gone:
            self.save()
        return gone

    # ---------- shops ----------
    def add_shop(self, name: str, url: str, zip_info: dict | None, all_products: bool) -> dict:
        key = _slug(name)
        self.shops = [s for s in self.shops if s["key"] != key]
        shop = {"key": key, "name": name, "url": url.strip(), "all": bool(all_products)}
        if zip_info:
            shop.update({"zip": zip_info["zip"], "lat": zip_info["lat"], "lon": zip_info["lon"],
                         "city": zip_info.get("city", ""), "state": zip_info.get("state", "")})
        self.shops.append(shop)
        self.save()
        return shop

    def remove_shop(self, name: str) -> bool:
        key = _slug(name)
        before = len(self.shops)
        self.shops = [s for s in self.shops if s["key"] != key]
        if len(self.shops) != before:
            self.save()
            return True
        return False

    # ---------- watched URLs ----------
    def add_url(self, url: str, name: str | None) -> dict:
        url = url.strip()
        key = _slug(url)
        self.urls = [u for u in self.urls if u["key"] != key]
        site = urlsplit(url).netloc.replace("www.", "")
        w = {"key": key, "url": url, "name": (name or "").strip(), "site": site}
        self.urls.append(w)
        self.save()
        return w

    def remove_url(self, url_or_name: str) -> bool:
        needle = url_or_name.strip().lower()
        before = len(self.urls)
        self.urls = [u for u in self.urls
                     if u["url"].lower() != needle and u.get("name", "").lower() != needle
                     and u["key"] != _slug(needle)]
        if len(self.urls) != before:
            self.save()
            return True
        return False

    # ---------- pinned SKUs / TCINs ----------
    def add_sku(self, retailer: str, sku_id: str, name: str = "") -> dict:
        sku_id = re.sub(r"\D", "", sku_id)   # "A-90777" (Target URL) or "6600009" → digits only
        self.skus = [x for x in self.skus if not (x["retailer"] == retailer and x["id"] == sku_id)]
        rec = {"retailer": retailer, "id": sku_id, "name": (name or "").strip(), "added": time.time()}
        self.skus.append(rec)
        self.save()
        return rec

    def remove_sku(self, sku_id: str) -> bool:
        sku_id = re.sub(r"\D", "", sku_id)
        before = len(self.skus)
        self.skus = [x for x in self.skus if x["id"] != sku_id]
        if len(self.skus) != before:
            self.save()
            return True
        return False

    # ---------- upcoming drops ----------
    def upsert_drop(self, name: str, date: str, game: str, source: str, url: str = "",
                    retailer: str = "", sku: str = "", key: str | None = None) -> tuple[dict, bool]:
        """Add or update a drop. Returns (record, is_new). Retailer-sourced drops key on retailer:sku."""
        key = key or (f"{retailer}:{sku}" if retailer and sku else "manual:" + _slug(name))
        for d in self.drops:
            if d["key"] == key:
                changed = (d.get("date") != date) or (d.get("name") != name and source != "manual")
                if source == "manual" or d.get("source") != "manual":
                    d.update({"name": name, "date": date, "game": game, "source": source,
                              "url": url or d.get("url", ""), "retailer": retailer or d.get("retailer", ""),
                              "sku": sku or d.get("sku", "")})
                    if changed:
                        self.save()
                return d, False
        rec = {"key": key, "name": name, "date": date, "game": game, "source": source,
               "url": url, "retailer": retailer, "sku": sku, "added": time.time()}
        self.drops.append(rec)
        self.save()
        return rec, True

    def remove_drop(self, name_or_key: str) -> bool:
        needle = name_or_key.strip().lower()
        before = len(self.drops)
        self.drops = [d for d in self.drops
                      if d["key"] != needle and d["name"].lower() != needle
                      and d["key"] != "manual:" + _slug(needle)]
        if len(self.drops) != before:
            self.save()
            return True
        return False

    def prune_drops(self, keep_days_past: int = 14) -> None:
        import datetime as _dt
        cutoff = (_dt.date.today() - _dt.timedelta(days=keep_days_past)).isoformat()
        before = len(self.drops)
        self.drops = [d for d in self.drops if (d.get("date") or "9999") >= cutoff]
        if len(self.drops) != before:
            self.save()

    def upcoming_drops(self, days: int = 60) -> list[dict]:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        horizon = (_dt.date.today() + _dt.timedelta(days=days)).isoformat()
        out = [d for d in self.drops if today <= (d.get("date") or "") <= horizon]
        return sorted(out, key=lambda d: (d["date"], d["name"]))

    # ---------- news feeds ----------
    def add_feed(self, url: str) -> bool:
        url = url.strip()
        if url in self.feeds:
            return False
        self.feeds.append(url)
        self.save()
        return True

    def remove_feed(self, url: str) -> bool:
        url = url.strip()
        if url not in self.feeds:
            return False
        self.feeds.remove(url)
        self.save()
        return True
