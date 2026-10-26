"""Drop news — reads RSS/Atom feeds (PokéBeach, YGOrganization, …) for upcoming releases,
restocks and pre-orders, and pulls a release date out of the headline when there is one.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape

import filters

log = logging.getLogger("news")

DEFAULT_FEEDS = [
    "https://www.pokebeach.com/feed",          # Pokémon TCG set reveals, release dates, product news
    "https://ygorganization.com/feed/",        # Yu-Gi-Oh! TCG product + release news
]

DEFAULT_KEYWORDS = [
    "release", "launch", "pre-order", "preorder", "restock", "street date", "elite trainer",
    "booster", "tin", "collection", "bundle", "drop", "coming", "announced", "revealed",
    "set list", "product", "deck", "box", "in stores", "available", "exclusive", "promo",
    "expansion", "anniversary", "special set", "mega", "premium",
]

MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
MONTHS_ABBR = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RX = re.compile(
    rf"\b(?P<m>{MONTHS}|{MONTHS_ABBR})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y>20\d\d))?\b"
    rf"|\b(?P<m2>\d{{1,2}})/(?P<d2>\d{{1,2}})/(?P<y2>20\d\d|\d\d)\b", re.I)
TAG_RX = re.compile(r"<[^>]+>")
_MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS.split("|"))}
_MONTH_NUM.update({m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())})
_MONTH_NUM["sept"] = 9


@dataclass
class NewsItem:
    key: str
    title: str
    link: str
    source: str
    published: float          # epoch
    summary: str
    game: str                 # pokemon | yugioh | other
    release_date: str = ""    # YYYY-MM-DD if we found one in the headline/summary


def extract_date(text: str, today: date | None = None) -> str:
    """First plausible future-ish date in the text (within the next ~14 months), as YYYY-MM-DD."""
    today = today or date.today()
    for m in DATE_RX.finditer(text or ""):
        try:
            if m.group("m"):
                mon = _MONTH_NUM[m.group("m").lower().rstrip(".")]
                day = int(m.group("d"))
                yr = int(m.group("y")) if m.group("y") else None
            else:
                mon, day = int(m.group("m2")), int(m.group("d2"))
                y = m.group("y2")
                yr = int(y) if len(y) == 4 else 2000 + int(y)
            if yr is None:  # pick the year that puts the date in the near future
                yr = today.year
                if date(yr, mon, day) < today - timedelta(days=45):
                    yr += 1
            d = date(yr, mon, day)
        except (ValueError, KeyError):
            continue
        if today - timedelta(days=14) <= d <= today + timedelta(days=430):
            return d.isoformat()
    return ""


def _text(el, *names) -> str:
    for n in names:
        found = el.find(n)
        if found is not None and (found.text or "").strip():
            return unescape(found.text.strip())
    return ""


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(TAG_RX.sub(" ", s or ""))).strip()


def parse_feed(xml_text: str, source: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as e:
        raise RuntimeError(f"{source}: not a valid RSS/Atom feed ({e})")
    ns = {"a": "http://www.w3.org/2005/Atom", "c": "http://purl.org/rss/1.0/modules/content/"}
    rss_items = root.findall("./channel/item")
    atom_entries = root.findall("a:entry", ns)
    if rss_items:
        for it in rss_items:
            title = _text(it, "title")
            link = _text(it, "link") or _text(it, "guid")
            summary = _strip_html(_text(it, "description") or _text(it, "c:encoded"))
            pub = _text(it, "pubDate")
            try:
                ts = parsedate_to_datetime(pub).timestamp() if pub else time.time()
            except Exception:
                ts = time.time()
            items.append(NewsItem(key=link or title, title=title, link=link, source=source,
                                  published=ts, summary=summary[:400], game="other"))
    elif atom_entries:
        A = "{http://www.w3.org/2005/Atom}"
        for it in atom_entries:
            title = _text(it, A + "title")
            link_el = it.find("a:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""
            summary = _strip_html(_text(it, A + "summary", A + "content"))
            upd = _text(it, A + "updated", A + "published")
            try:
                ts = datetime.fromisoformat(upd.replace("Z", "+00:00")).timestamp() if upd else time.time()
            except Exception:
                ts = time.time()
            items.append(NewsItem(key=link or title, title=title, link=link, source=source,
                                  published=ts, summary=summary[:400], game="other"))
    else:
        raise RuntimeError(f"{source}: feed has no items")
    for n in items:
        n.game = filters.which_game(f"{n.title} {n.summary}") or ("yugioh" if "ygorg" in source else
                                                                   "pokemon" if "pokebeach" in source else "other")
        n.release_date = extract_date(n.title) or extract_date(n.summary[:300])
    return items


class NewsChecker:
    name = "news"
    label = "Drop news"

    def __init__(self, fetcher, keywords: list[str] | None = None, refresh_min: int = 30):
        self.fetcher = fetcher
        self.keywords = [k.lower() for k in (keywords or DEFAULT_KEYWORDS)]
        self.refresh_s = refresh_min * 60
        self._last_fetch = 0.0
        self._cache: list[NewsItem] = []
        self.last_summary = ""
        self.errors: dict[str, str] = {}

    def relevant(self, n: NewsItem) -> bool:
        text = f"{n.title} {n.summary}".lower()
        return any(k in text for k in self.keywords)

    async def fetch_all(self, feeds: list[str]) -> list[NewsItem]:
        if self._cache and time.time() - self._last_fetch < self.refresh_s:
            return self._cache
        out: list[NewsItem] = []
        self.errors = {}
        for url in feeds:
            source = re.sub(r"^www\.", "", url.split("/")[2]) if "//" in url else url
            status, text, _ = await self.fetcher.get_text(url, headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"})
            if status != 200 or not text:
                self.errors[source] = f"HTTP {status}"
                continue
            try:
                out += [n for n in parse_feed(text, source) if self.relevant(n)]
            except RuntimeError as e:
                self.errors[source] = str(e)
        self._cache = sorted(out, key=lambda n: n.published, reverse=True)
        self._last_fetch = time.time()
        ok = len(feeds) - len(self.errors)
        self.last_summary = f"{ok}/{len(feeds)} feeds ok · {len(self._cache)} relevant articles"
        if feeds and ok == 0:
            raise RuntimeError("every news feed failed: " + "; ".join(f"{k}: {v}" for k, v in self.errors.items()))
        return self._cache
