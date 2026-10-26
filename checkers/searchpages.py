"""Walmart + GameStop — reads their search-results pages for Pokémon / Yu-Gi-Oh! product.

Online stock only (neither site exposes per-store shelf counts to scripts), best-effort:
both use bot protection, so a run can come back "blocked" — that's reported in the
summary, never silently treated as "nothing in stock".

Walmart: the search page embeds a JSON blob (__NEXT_DATA__) with every item's
availability. GameStop: product tiles carry a data-gtmdata JSON with availability.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from html import unescape

import filters
from models import Hit, Product

log = logging.getLogger("searchpages")

QUERIES = {
    "pokemon": ["pokemon trading cards"],
    "yugioh": ["yu-gi-oh trading cards"],
}
BLOCK_RX = re.compile(r"px-captcha|Robot or human|Access Denied|blocked|captcha", re.I)
NEXT_DATA_RX = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
GS_TILE_RX = re.compile(r'data-gtmdata="([^"]+)"', re.I)
GS_LINK_RX = re.compile(r'href="(/products/[^"]+?/(\d+)\.html)"', re.I)


def _walk(obj, want_key: str):
    """Yield every value found under a given key anywhere in a nested JSON structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == want_key:
                yield v
            else:
                yield from _walk(v, want_key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, want_key)


def parse_walmart(html: str, game: str) -> tuple[str, list[Product], set[str]]:
    """Return (state, products, in_stock_ids). state ∈ ok | blocked | unknown."""
    if BLOCK_RX.search(html[:5000]) and "__NEXT_DATA__" not in html:
        return "blocked", [], set()
    m = NEXT_DATA_RX.search(html)
    if not m:
        return "unknown", [], set()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return "unknown", [], set()
    products: dict[str, Product] = {}
    in_stock: set[str] = set()
    for items in _walk(data, "items"):
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("usItemId") or it.get("id") or "")
            name = it.get("name") or ""
            if not iid or not name or filters.match(name, {game}) is None:
                continue
            seller = (it.get("sellerName") or "").lower()
            if seller and "walmart" not in seller:
                continue  # marketplace scalper listing
            url = it.get("canonicalUrl") or f"/ip/{iid}"
            price = it.get("price") or (it.get("priceInfo") or {}).get("linePrice") or ""
            products[iid] = Product(retailer="walmart", id=iid, title=name, game=game,
                                    url="https://www.walmart.com" + url if url.startswith("/") else url,
                                    price=str(price) if price else "",
                                    image=(it.get("imageInfo") or {}).get("thumbnailUrl") or it.get("image") or "")
            avail = (it.get("availabilityStatusV2") or {}).get("value") or it.get("availabilityStatus") or ""
            if str(avail).upper() in ("IN_STOCK", "LIMITED_STOCK"):
                in_stock.add(iid)
    return "ok", list(products.values()), in_stock


def parse_gamestop(html: str, game: str) -> tuple[str, list[Product], set[str]]:
    if BLOCK_RX.search(html[:5000]) and "product-tile" not in html and "data-gtmdata" not in html:
        return "blocked", [], set()
    products: dict[str, Product] = {}
    in_stock: set[str] = set()
    names: dict[str, str] = {}
    for m in GS_TILE_RX.finditer(html):
        try:
            d = json.loads(unescape(m.group(1)))
        except json.JSONDecodeError:
            continue
        sku = str(d.get("id") or d.get("productID") or d.get("sku") or "")
        name = d.get("name") or d.get("productName") or ""
        if not sku or not name or filters.match(name, {game}) is None:
            continue
        names[sku] = name
        avail = str(d.get("availability") or d.get("productAvailability") or "").lower()
        price = d.get("price") or ""
        products[sku] = Product(retailer="gamestop", id=sku, title=name, game=game,
                                url=f"https://www.gamestop.com/search/?q={sku}", price=f"${price}" if price else "")
        if avail and avail not in ("not available", "unavailable", "out of stock", "sold out", "notavailable"):
            in_stock.add(sku)
    for m in GS_LINK_RX.finditer(html):
        sku = m.group(2)
        if sku in products:
            products[sku].url = "https://www.gamestop.com" + m.group(1)
    if not products and "product-tile" not in html:
        return "unknown", [], set()
    return "ok", list(products.values()), in_stock


class SearchPageChecker:
    name = "searchpages"
    label = "Walmart + GameStop"

    def __init__(self, fetcher, walmart: bool = True, gamestop: bool = True, delay: float = 1.5):
        self.fetcher = fetcher
        self.sites = [s for s, on in (("walmart", walmart), ("gamestop", gamestop)) if on]
        self.delay = delay
        self.known_products: dict[str, Product] = {}
        self.last_summary = ""

    async def scan(self, trackers: list[dict], **_) -> list[Hit]:
        games = set()
        for t in trackers:
            games.update(t.get("games") or [])
        games = games or {"pokemon", "yugioh"}
        hits: list[Hit] = []
        status: dict[str, str] = {}
        known: dict[str, Product] = {}
        for site in self.sites:
            states, found = [], 0
            for game in games:
                for q in QUERIES[game]:
                    if site == "walmart":
                        url = "https://www.walmart.com/search?q=" + q.replace(" ", "+")
                        code, html, final = await self.fetcher.get_text(url)
                        state, prods, ok_ids = ("blocked", [], set()) if code in (0, 403, 429) else parse_walmart(html, game)
                    else:
                        url = "https://www.gamestop.com/search/?q=" + q.replace(" ", "%20")
                        code, html, final = await self.fetcher.get_text(url)
                        state, prods, ok_ids = ("blocked", [], set()) if code in (0, 403, 429) else parse_gamestop(html, game)
                    states.append(state)
                    for p in prods:
                        known[p.key] = p
                        if p.id in ok_ids:
                            hits.append(Hit(product=p, store=None, note=f"online at {site}.com"))
                    found += len(prods)
                    await asyncio.sleep(self.delay)
            if all(s == "blocked" for s in states):
                status[site] = "blocked"
            elif all(s == "unknown" for s in states):
                status[site] = "page changed?"
            else:
                status[site] = f"{found} products"
        self.known_products = known
        self.last_summary = " · ".join(f"{s}: {status[s]}" for s in self.sites) + f" · {len(hits)} in-stock online"
        if self.sites and all(status[s] in ("blocked", "page changed?") for s in self.sites):
            raise RuntimeError(self.last_summary + " (bot protection — expected now and then)")
        return hits
