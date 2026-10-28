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
    "funko": ["funko pop"],
}
SEARCH_URLS = {
    "walmart": "https://www.walmart.com/search?q={q}",
    "gamestop": "https://www.gamestop.com/search/?q={q}",
    "hottopic": "https://www.hottopic.com/search?q={q}&sz=120",   # Funko only
    # FYE runs on Shopify: its search endpoint returns JSON with real availability
    "fye": "https://www.fye.com/search/suggest.json?q={q}&resources[type]=product&resources[limit]=10"
           "&resources[options][fields]=title,product_type,vendor",
}
HT_TILE_RX = re.compile(r'<div class="product"\s+data-pid="(\d+)"(.*?)(?=<div class="product"\s+data-pid=|$)', re.S)
HT_NAME_RX = re.compile(r'<a class="pdpLink"\s+href="([^"]+)"\s+aria-label="([^"]+)"', re.S)
HT_PRICE_RX = re.compile(r'\$\s?(\d+\.\d{2})')
FUNKO_ONLY_SITES = {"hottopic", "fye"}
TILE_RX = re.compile(r'<(?:div|li)[^>]+class="[^"]*product(?:-tile|-grid-item|Tile)[^"]*"[^>]*>(.*?)(?=<(?:div|li)[^>]+class="[^"]*product(?:-tile|-grid-item|Tile)|$)', re.S | re.I)
TILE_NAME_RX = re.compile(r'(?:class="[^"]*(?:pdp-link|product-name|link|name)[^"]*"[^>]*>\s*<a[^>]*>|<a[^>]+class="[^"]*(?:link|name)[^"]*"[^>]*>|title="|aria-label=")([^"<]{6,160})', re.I)
TILE_HREF_RX = re.compile(r'href="(/(?:products?|p)/[^"]+)"', re.I)
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


def parse_hottopic(html: str, game: str) -> tuple[str, list[Product], set[str]]:
    """Hot Topic search: <div class="product" data-pid=…><a class="pdpLink" href aria-label=NAME>.
    Tiles carry no stock flag — sold-out items drop out of search — so listed means purchasable."""
    if 'class="product"' not in html and 'data-pid=' not in html:
        return ("blocked" if BLOCK_RX.search(html[:5000]) else "unknown"), [], set()
    products: dict[str, Product] = {}
    in_stock: set[str] = set()
    for m in HT_TILE_RX.finditer(html):
        pid, tile = m.group(1), m.group(2)
        nm = HT_NAME_RX.search(tile)
        if not nm:
            continue
        name = unescape(nm.group(2)).strip()
        if filters.match(name, {game}) is None:
            continue
        pm = HT_PRICE_RX.search(tile)
        img = re.search(r'(https://cdn\.media\.amplience\.net/s/hottopic/\d+_hi)', tile)
        products[pid] = Product(retailer="hottopic", id=pid, title=name, game=game,
                                url="https://www.hottopic.com" + nm.group(1).split("?")[0],
                                price=f"${pm.group(1)}" if pm else "", image=(img.group(1) + "?$pdpMainImage$") if img else "")
        if not re.search(r"sold out|out of stock|coming soon", tile, re.I):
            in_stock.add(pid)
    return "ok", list(products.values()), in_stock


def parse_fye(data, game: str) -> tuple[str, list[Product], set[str]]:
    """FYE (Shopify) search/suggest.json → products[] with available, price, image, url."""
    if not isinstance(data, dict):
        return "blocked", [], set()
    items = ((data.get("resources") or {}).get("results") or {}).get("products")
    if items is None:
        return "unknown", [], set()
    products: dict[str, Product] = {}
    in_stock: set[str] = set()
    for it in items:
        pid = str(it.get("id") or "")
        name = it.get("title") or ""
        if not pid or filters.match(name, {game}) is None:
            continue
        products[pid] = Product(retailer="fye", id=pid, title=name, game=game,
                                url="https://www.fye.com" + (it.get("url") or "").split("?")[0],
                                price=f"${it.get('price')}" if it.get("price") else "", image=it.get("image") or "")
        if it.get("available"):
            in_stock.add(pid)
    return "ok", list(products.values()), in_stock


def parse_tiles(html: str, game: str, site: str) -> tuple[str, list[Product], set[str]]:
    """Generic parser for Salesforce-Commerce-style shops (Hot Topic, FYE): product tiles with a name,
    a link, and 'Add to Cart' vs 'Sold Out' text. Best-effort."""
    if BLOCK_RX.search(html[:5000]) and "product" not in html[:200000].lower():
        return "blocked", [], set()
    products: dict[str, Product] = {}
    in_stock: set[str] = set()
    base = {"hottopic": "https://www.hottopic.com", "fye": "https://www.fye.com"}.get(site, "")
    for m in TILE_RX.finditer(html):
        tile = m.group(1)
        nm = TILE_NAME_RX.search(tile)
        href = TILE_HREF_RX.search(tile)
        name = unescape(nm.group(1)).strip() if nm else ""
        if not name or not href or filters.match(name, {game}) is None:
            continue
        pid = re.sub(r"[^0-9A-Za-z]", "", href.group(1).rsplit("/", 1)[-1].split(".")[0])[:40] or name[:40]
        price_m = re.search(r"\$\s?(\d+\.\d{2})", tile)
        products[pid] = Product(retailer=site, id=pid, title=name, game=game, url=base + href.group(1),
                                price=f"${price_m.group(1)}" if price_m else "")
        low = tile.lower()
        if ("add to cart" in low or "add to bag" in low or "in stock" in low) and not any(
                w in low for w in ("sold out", "out of stock", "notify me", "coming soon")):
            in_stock.add(pid)
    if not products and not TILE_RX.search(html):
        return "unknown", [], set()
    return "ok", list(products.values()), in_stock


class SearchPageChecker:
    name = "searchpages"
    label = "Walmart / GameStop / Hot Topic / FYE"

    def __init__(self, fetcher, walmart: bool = True, gamestop: bool = True, hottopic: bool = True,
                 fye: bool = True, delay: float = 1.5):
        self.fetcher = fetcher
        self.sites = [s for s, on in (("walmart", walmart), ("gamestop", gamestop),
                                      ("hottopic", hottopic), ("fye", fye)) if on]
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
            site_games = [g for g in games if g == "funko" or site not in FUNKO_ONLY_SITES]
            if not site_games:
                continue
            for game in site_games:
                for q in QUERIES[game]:
                    url = SEARCH_URLS[site].format(q=q.replace(" ", "+" if site == "walmart" else "%20"))
                    if site == "fye":
                        code, data = await self.fetcher.get_json(url)
                        html = ""
                    else:
                        code, html, final = await self.fetcher.get_text(url)
                    if code in (0, 403, 429, 435):
                        state, prods, ok_ids = "blocked", [], set()
                    elif site == "walmart":
                        state, prods, ok_ids = parse_walmart(html, game)
                    elif site == "gamestop":
                        state, prods, ok_ids = parse_gamestop(html, game)
                    elif site == "hottopic":
                        state, prods, ok_ids = parse_hottopic(html, game)
                    elif site == "fye":
                        state, prods, ok_ids = parse_fye(data, game)
                    else:
                        state, prods, ok_ids = parse_tiles(html, game, site)
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
        self.last_summary = " · ".join(f"{s}: {status[s]}" for s in self.sites if s in status) + f" · {len(hits)} in-stock online"
        if status and all(status[s] in ("blocked", "page changed?") for s in status):
            raise RuntimeError(self.last_summary + " (bot protection — expected now and then)")
        return hits
