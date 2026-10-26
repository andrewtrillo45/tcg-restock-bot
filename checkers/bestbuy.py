"""Best Buy — official developer API (free key from https://developer.bestbuy.com).

  /v1/products(search=…)            → product list with online + in-store flags
  /v1/products/{sku}/stores.json    → stores near a ZIP that have the SKU for pickup
Free keys allow ~5 calls/second and 50,000/day; we stay far under that.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

import filters
from geo import lookup_zip
from models import Hit, Product, Store

log = logging.getLogger("bestbuy")
API = "https://api.bestbuy.com/v1"
SHOW = ("sku,name,salePrice,regularPrice,url,addToCartUrl,inStoreAvailability,"
        "onlineAvailability,image,thumbnailImage,releaseDate,preowned")


class BestBuyChecker:
    name = "bestbuy"
    label = "Best Buy"

    def __init__(self, fetcher, api_key: str, delay: float = 0.3, product_cache_hours: int = 6,
                 max_products: int = 80):
        self.fetcher = fetcher
        self.api_key = api_key
        self.delay = delay
        self.max_products = max_products
        self._product_cache: dict[str, tuple[float, list[Product]]] = {}
        self._product_ttl = product_cache_hours * 3600
        self._flags: dict[str, dict] = {}     # sku → {"online": bool, "in_store": bool}
        self._pinned_cache: dict[str, tuple[float, Product | None]] = {}
        self.known_products: dict[str, Product] = {}   # sku → Product, refreshed each scan
        self.last_summary = ""
        self.calls = 0

    async def search(self, terms: list[str], game: str) -> list[Product]:
        ck = "+".join(terms)
        cached = self._product_cache.get(ck)
        if cached and time.time() - cached[0] < self._product_ttl:
            return cached[1]
        query = "&".join(f"search={quote(t)}" for t in terms)
        url = f"{API}/products({query})"
        params = {
            "apiKey": self.api_key, "format": "json", "pageSize": 100,
            "show": SHOW,
        }
        self.calls += 1
        status, data = await self.fetcher.get_json(url, params=params)
        self._raise_for(status)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"search returned HTTP {status}")
        products: list[Product] = []
        for p in data.get("products") or []:
            prod = self._parse(p, game, strict=True)
            if prod:
                products.append(prod)
        products = products[: self.max_products]
        self._product_cache[ck] = (time.time(), products)
        return products

    @staticmethod
    def _raise_for(status: int) -> None:
        if status == 403:
            raise RuntimeError("HTTP 403 — API key invalid or not active yet")
        if status == 429:
            raise RuntimeError("HTTP 429 — rate limited, slow the scan interval")

    def _parse(self, p: dict, game: str | None, strict: bool) -> Product | None:
        sku = str(p.get("sku") or "")
        title = p.get("name") or ""
        if not sku or not title or p.get("preowned"):
            return None
        if strict and filters.match(title, {game} if game else None) is None:
            return None
        game = game or filters.which_game(title) or "other"
        self._flags[sku] = {"online": bool(p.get("onlineAvailability")),
                            "in_store": bool(p.get("inStoreAvailability"))}
        price = p.get("salePrice") or p.get("regularPrice")
        rd = p.get("releaseDate") or ""
        return Product(
            retailer="bestbuy", id=sku, title=title, game=game,
            url=p.get("url") or f"https://www.bestbuy.com/site/{sku}.p",
            price=f"${price:.2f}" if isinstance(price, (int, float)) else "",
            image=p.get("thumbnailImage") or p.get("image") or "",
            release_date=rd[:10] if isinstance(rd, str) else "",
        )

    async def product(self, sku: str) -> Product | None:
        """Look one SKU up directly (for products pinned with /addsku)."""
        cached = self._pinned_cache.get(sku)
        if cached and time.time() - cached[0] < self._product_ttl:
            return cached[1]
        self.calls += 1
        status, data = await self.fetcher.get_json(f"{API}/products/{sku}.json",
                                                   params={"apiKey": self.api_key, "show": SHOW})
        self._raise_for(status)
        prod = self._parse(data, None, strict=False) if status == 200 and isinstance(data, dict) else None
        self._pinned_cache[sku] = (time.time(), prod)
        return prod

    async def stores_with_stock(self, sku: str, zip_code: str, radius: int) -> list[Store]:
        params = {"apiKey": self.api_key, "postalCode": zip_code, "distance": radius}
        self.calls += 1
        status, data = await self.fetcher.get_json(f"{API}/products/{sku}/stores.json", params=params)
        if status == 429:
            raise RuntimeError("HTTP 429 — rate limited")
        if status != 200 or not isinstance(data, dict):
            return []
        out = []
        for s in data.get("stores") or []:
            sid = str(s.get("storeId") or s.get("storeID") or "")
            if not sid:
                continue
            lat, lon = s.get("lat"), s.get("lng")
            try:
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                lat = lon = None
            st = Store(retailer="bestbuy", id=sid, name=f"Best Buy {s.get('name') or sid}",
                       city=s.get("city", ""), state=s.get("region", ""),
                       zip=str(s.get("postalCode", "") or "")[:5], address=s.get("address", ""),
                       lat=lat, lon=lon)
            if st.lat is None and st.zip:
                info = await lookup_zip(self.fetcher, st.zip)
                if info:
                    st.lat, st.lon = info["lat"], info["lon"]
            out.append(st)
        return out

    async def scan(self, trackers: list[dict], skus: list[dict] | None = None, **_) -> list[Hit]:
        if not self.api_key:
            raise RuntimeError("no BESTBUY_API_KEY set")
        pinned = [s["id"] for s in (skus or []) if s.get("retailer") == "bestbuy"]
        trackers = [t for t in trackers if t.get("lat") is not None]
        games = set()
        for t in trackers:
            games.update(t.get("games") or [])
        if not games:
            games = {"pokemon", "yugioh"}
        self.calls = 0

        products: dict[str, Product] = {}
        for game in games:
            for terms in filters.GAMES[game]["search"]["bestbuy"]:
                for p in await self.search(terms, game):
                    products.setdefault(p.id, p)
                await asyncio.sleep(self.delay)
        for sku in pinned:
            if sku in products:
                continue
            prod = await self.product(sku)
            products[sku] = prod or Product(retailer="bestbuy", id=sku, title=f"Best Buy item {sku}",
                                            game="other", url=f"https://www.bestbuy.com/site/{sku}.p")
            await asyncio.sleep(self.delay)
        self.known_products = dict(products)

        hits: list[Hit] = []
        # online availability comes free with the search
        for sku, prod in products.items():
            if self._flags.get(sku, {}).get("online"):
                hits.append(Hit(product=prod, store=None, note="online (ships)"))

        # local pickup: one query per (zip, radius) circle, only for SKUs sold in stores
        circles = {(t["zip"], int(t.get("radius") or 50)) for t in trackers}
        seen: set[str] = set()
        for sku, prod in products.items():
            if not self._flags.get(sku, {}).get("in_store"):
                continue
            for zip_code, radius in circles:
                for st in await self.stores_with_stock(sku, zip_code, radius):
                    k = f"{sku}@{st.id}"
                    if k in seen:
                        continue
                    seen.add(k)
                    hits.append(Hit(product=prod, store=st, note="store pickup"))
                await asyncio.sleep(self.delay)
        self.last_summary = (f"{len(products)} products · {len(circles)} area(s) · "
                             f"{len(hits)} in-stock · {self.calls} calls")
        return hits
