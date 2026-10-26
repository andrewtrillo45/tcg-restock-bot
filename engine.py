"""Runs every checker, works out what is NEWLY in stock, and who should hear about it."""
from __future__ import annotations

import json
import logging
import os
import time

from datetime import date

from checkers.news import DEFAULT_FEEDS, NewsItem
from geo import haversine_mi
from models import Alert, Hit, Product

log = logging.getLogger("engine")


class Engine:
    def __init__(self, state, checkers: list, seen_path: str, realert_hours: float = 6.0,
                 news=None):
        self.state = state
        self.checkers = checkers
        self.news = news                              # NewsChecker or None
        self.seen_path = seen_path
        self.realert_s = realert_hours * 3600
        self.in_stock: dict[str, set[str]] = {}       # checker name → keys currently in stock
        self.last_alerted: dict[str, float] = {}      # key → epoch of last alert
        self.known_listings: set[str] = set()         # product keys we have seen listed at retailers
        self.seen_news: set[str] = set()              # article links already posted
        self.health: dict[str, tuple[str, str]] = {}  # checker → ("ok"|"error"|"idle", detail)
        self.last_hits: list[Hit] = []
        self.new_listings: list[Product] = []         # filled by scan(): brand-new product pages
        self.new_drops: list[dict] = []               # filled by scan(): drops added/updated this pass
        self.last_scan_at: float | None = None
        self.last_scan_secs: float = 0
        self.scans = 0
        self.news_baselined = False
        self.baselined = self._load_seen()

    # ---------- dedupe memory ----------
    def _load_seen(self) -> bool:
        try:
            with open(self.seen_path, encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - float(d.get("saved_at", 0)) > 36 * 3600:
                return False
            self.in_stock = {k: set(v) for k, v in (d.get("in_stock") or {}).items()}
            self.last_alerted = {k: float(v) for k, v in (d.get("last_alerted") or {}).items()}
            self.known_listings = set(d.get("known_listings") or [])
            self.seen_news = set(d.get("seen_news") or [])
            self.news_baselined = bool(self.seen_news)
            return True
        except Exception:
            return False

    def _save_seen(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.seen_path) or ".", exist_ok=True)
            cutoff = time.time() - 7 * 86400
            with open(self.seen_path, "w", encoding="utf-8") as f:
                json.dump({"saved_at": time.time(),
                           "in_stock": {k: sorted(v) for k, v in self.in_stock.items()},
                           "last_alerted": {k: v for k, v in self.last_alerted.items() if v > cutoff},
                           "known_listings": sorted(self.known_listings),
                           "seen_news": sorted(self.seen_news)[-500:]},
                          f)
        except Exception as e:
            log.warning("could not save seen-state: %s", e)

    # ---------- the scan ----------
    async def scan(self, force_alert: bool = False) -> list[Alert]:
        t0 = time.time()
        trackers = list(self.state.trackers.values())
        new_hits: list[Hit] = []
        all_hits: list[Hit] = []
        self.new_listings = []
        self.new_drops = []
        for ch in self.checkers:
            try:
                hits = await ch.scan(trackers, shops=self.state.shops, urls=self.state.urls,
                                     skus=self.state.skus)
                self.health[ch.name] = ("ok", ch.last_summary or f"{len(hits)} in stock")
            except Exception as e:  # one broken retailer must not stop the others
                log.exception("%s failed", ch.name)
                self.health[ch.name] = ("error", str(e)[:200])
                continue
            all_hits += hits
            self._harvest_listings(ch)
            keys_now = {h.key for h in hits}
            before = self.in_stock.get(ch.name, set())
            self.in_stock[ch.name] = keys_now
            if self.baselined or force_alert:
                for h in hits:
                    recently = time.time() - self.last_alerted.get(h.key, 0) < self.realert_s
                    if (h.key not in before or force_alert) and not recently:
                        new_hits.append(h)
        self.last_hits = all_hits
        self.last_scan_at = time.time()
        self.last_scan_secs = self.last_scan_at - t0
        self.scans += 1
        first_pass = not self.baselined
        self.baselined = True
        self._save_seen()
        if first_pass and not force_alert:
            log.info("baseline scan done: %d items in stock, no alerts sent", len(all_hits))
            return []
        alerts = self._build_alerts(new_hits, trackers)
        for a in alerts:
            for h in a.hits:
                self.last_alerted[h.key] = time.time()
        if alerts:
            self._save_seen()
        return alerts

    # ---------- new product pages & street dates ----------
    def _harvest_listings(self, ch) -> None:
        known = getattr(ch, "known_products", None)
        if not known:
            return
        first_time = not self.known_listings or not self.baselined
        today = date.today().isoformat()
        for prod in known.values():
            if prod.key not in self.known_listings:
                self.known_listings.add(prod.key)
                if not first_time and prod.game != "other":
                    self.new_listings.append(prod)
            if prod.release_date and prod.release_date >= today and prod.game != "other":
                rec, is_new = self.state.upsert_drop(prod.title, prod.release_date, prod.game,
                                                     source=ch.name, url=prod.url,
                                                     retailer=ch.name, sku=prod.id)
                if is_new and not first_time:
                    self.new_drops.append(rec)

    # ---------- news ----------
    def feeds(self) -> list[str]:
        return list(dict.fromkeys(DEFAULT_FEEDS + list(self.state.feeds)))

    async def scan_news(self) -> list[NewsItem]:
        """Return articles not posted before. First run only records what exists (no spam)."""
        if self.news is None:
            return []
        try:
            items = await self.news.fetch_all(self.feeds())
            self.health["news"] = ("ok", self.news.last_summary)
        except Exception as e:
            self.health["news"] = ("error", str(e)[:200])
            return []
        fresh = [n for n in items if n.key not in self.seen_news]
        for n in fresh:
            self.seen_news.add(n.key)
        if not self.news_baselined:
            self.news_baselined = True
            self._save_seen()
            return []
        cutoff = time.time() - 3 * 86400          # never post week-old articles after a restart
        fresh = [n for n in fresh if n.published >= cutoff]
        if fresh:
            self._save_seen()
        return sorted(fresh, key=lambda n: n.published)

    # ---------- routing ----------
    def _build_alerts(self, hits: list[Hit], trackers: list[dict]) -> list[Alert]:
        by_product: dict[str, list[Hit]] = {}
        for h in hits:
            by_product.setdefault(h.product.key, []).append(h)
        alerts: list[Alert] = []
        for pkey, group in by_product.items():
            local = [h for h in group if h.store is not None]
            online = [h for h in group if h.store is None]
            prod = group[0].product
            if local:
                recips = []
                for t in trackers:
                    if prod.game not in (t.get("games") or []) and prod.game != "other":
                        continue
                    if t.get("lat") is None:
                        continue
                    dists = [haversine_mi(t["lat"], t["lon"], h.store.lat, h.store.lon)
                             for h in local if h.store.lat is not None]
                    if not dists:
                        continue
                    nearest = min(dists)
                    if nearest <= float(t.get("radius") or 50):
                        recips.append((t["user_id"], round(nearest, 1)))
                # keep only stores inside at least one recipient's circle
                kept = []
                for h in local:
                    if h.store.lat is None:
                        continue
                    if any(haversine_mi(t["lat"], t["lon"], h.store.lat, h.store.lon)
                           <= float(t.get("radius") or 50)
                           for t in trackers if t.get("lat") is not None):
                        kept.append(h)
                if kept:
                    alerts.append(Alert(kind="local", product=prod, hits=kept, recipients=recips))
            if online:
                recips = [(t["user_id"], None) for t in trackers
                          if t.get("online", True)
                          and (prod.game in (t.get("games") or []) or prod.game == "other")]
                alerts.append(Alert(kind="online", product=prod, hits=online, recipients=recips))
        return alerts

    # ---------- for /stock ----------
    def stock_for(self, tracker: dict) -> tuple[list[tuple[Hit, float]], list[Hit]]:
        local, online = [], []
        for h in self.last_hits:
            if h.product.game not in (tracker.get("games") or []) and h.product.game != "other":
                continue
            if h.store is None:
                online.append(h)
            elif h.store.lat is not None and tracker.get("lat") is not None:
                d = haversine_mi(tracker["lat"], tracker["lon"], h.store.lat, h.store.lon)
                if d <= float(tracker.get("radius") or 50):
                    local.append((h, round(d, 1)))
        local.sort(key=lambda x: x[1])
        return local, online
