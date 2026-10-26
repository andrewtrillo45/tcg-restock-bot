"""Generic product-page watcher (Walmart, GameStop, Costco, Pokémon Center, anything).

These sites don't expose local store stock to scripts and often block bots, so this is
best-effort ONLINE stock only: we fetch the page and look for the usual in-stock
signals (schema.org availability, Walmart's availabilityStatus, add-to-cart text).
A blocked page (403/429/captcha) is reported in /status rather than alerting.
"""
from __future__ import annotations

import asyncio
import logging
import re

import filters
from models import Hit, Product

log = logging.getLogger("pagewatch")

IN_STOCK_SIGNALS = [
    r'schema\.org/InStock', r'"availability"\s*:\s*"InStock"',
    r'"availabilityStatus"\s*:\s*"IN_STOCK"', r'"availabilityStatusDisplayValue"\s*:\s*"In stock"',
    r'"inStock"\s*:\s*true', r'"is_in_stock"\s*:\s*true', r'"available"\s*:\s*true',
    r'"availability"\s*:\s*"IN_STOCK"',
]
OUT_SIGNALS = [
    r'schema\.org/(OutOfStock|SoldOut|Discontinued)', r'"availability"\s*:\s*"(OutOfStock|SoldOut)"',
    r'"availabilityStatus"\s*:\s*"(OUT_OF_STOCK|NOT_AVAILABLE)"', r'"inStock"\s*:\s*false',
    r'"available"\s*:\s*false', r'"availability"\s*:\s*"OUT_OF_STOCK"',
]
BLOCK_SIGNALS = [r'captcha', r'px-captcha', r'Access Denied', r'queue-it\.net', r'Robot or human']
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_IMAGE_RE = re.compile(r'property="og:image"\s+content="([^"]+)"', re.I)


def classify(status: int, html: str, final_url: str) -> tuple[str, str]:
    """Return (state, title) where state ∈ in_stock | out | blocked | unknown."""
    title = ""
    m = TITLE_RE.search(html or "")
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    if status in (403, 429, 503) or (status == 0):
        return "blocked", title
    if "queue-it" in (final_url or "") or any(re.search(p, html[:200000], re.I) for p in BLOCK_SIGNALS[-2:]):
        return "blocked", title
    body = html[:600000]
    if any(re.search(p, body, re.I) for p in BLOCK_SIGNALS[:3]) and len(body) < 20000:
        return "blocked", title
    in_stock = any(re.search(p, body) for p in IN_STOCK_SIGNALS)
    out = any(re.search(p, body) for p in OUT_SIGNALS)
    if in_stock and not out:
        return "in_stock", title
    if out and not in_stock:
        return "out", title
    if in_stock and out:
        # mixed signals (variants) — treat an explicit add-to-cart as in stock
        if re.search(r'add to cart', body, re.I) and not re.search(r'sold out|out of stock', body, re.I):
            return "in_stock", title
        return "unknown", title
    return "unknown", title


class PageWatchChecker:
    name = "urls"
    label = "Watched product pages"

    def __init__(self, fetcher, delay: float = 1.0):
        self.fetcher = fetcher
        self.delay = delay
        self.last_summary = ""
        self.states: dict[str, str] = {}

    async def scan(self, trackers: list[dict], urls: list[dict] | None = None, **_) -> list[Hit]:
        urls = urls or []
        hits: list[Hit] = []
        blocked = 0
        for w in urls:
            status, html, final = await self.fetcher.get_text(w["url"])
            state, title = classify(status, html, final)
            self.states[w["key"]] = state
            if state == "blocked":
                blocked += 1
            elif state == "in_stock":
                name = w.get("name") or title or w["url"]
                game = filters.which_game(name) or "other"
                image = ""
                m = OG_IMAGE_RE.search(html or "")
                if m:
                    image = m.group(1)
                prod = Product(retailer="url", id=w["key"], title=name, game=game,
                               url=w["url"], image=image)
                hits.append(Hit(product=prod, store=None, note=f"online at {w.get('site', 'site')}"))
            await asyncio.sleep(self.delay)
        self.last_summary = f"{len(urls)} pages · {len(hits)} in-stock · {blocked} blocked"
        if urls and blocked == len(urls):
            raise RuntimeError("every watched page is blocking us (try fewer pages or a longer interval)")
        return hits
