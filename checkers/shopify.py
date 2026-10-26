"""Local card shops that run on Shopify (most do).

Every Shopify store exposes  <shop>/products.json  and  <shop>/collections/<x>/products.json
publicly. We watch for sealed Pokémon / Yu-Gi-Oh! product that is purchasable.
If the shop was added with a ZIP, hits are treated as local (distance-filtered);
otherwise they are online alerts.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

import filters
from models import Hit, Product, Store

log = logging.getLogger("shopify")


def products_json_url(url: str) -> str:
    """Turn any shop / collection URL into its products.json endpoint."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    if path.endswith("/products.json"):
        pass
    elif "/collections/" in path:
        path = path.split("/products")[0] + "/products.json"
    else:
        path = "/products.json"
    return urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))


class ShopifyChecker:
    name = "shops"
    label = "Local card shops"

    def __init__(self, fetcher, delay: float = 0.5, max_pages: int = 8):
        self.fetcher = fetcher
        self.delay = delay
        self.max_pages = max_pages
        self.last_summary = ""
        self.errors: dict[str, str] = {}

    async def scan_shop(self, shop: dict) -> list[Hit]:
        url = products_json_url(shop["url"])
        store = Store(retailer="shop", id=shop["key"], name=shop["name"],
                      city=shop.get("city", ""), state=shop.get("state", ""),
                      zip=shop.get("zip", ""), lat=shop.get("lat"), lon=shop.get("lon"))
        local = store.lat is not None
        hits: list[Hit] = []
        for page in range(1, self.max_pages + 1):
            status, data = await self.fetcher.get_json(url, params={"limit": 250, "page": page})
            if status in (401, 403, 404, 430):
                raise RuntimeError(f"HTTP {status} from {urlsplit(url).netloc} (not Shopify, or blocked)")
            if status != 200 or not isinstance(data, dict):
                raise RuntimeError(f"HTTP {status} from {urlsplit(url).netloc}")
            items = data.get("products") or []
            if not items:
                break
            for p in items:
                title = p.get("title") or ""
                game = filters.match(title, require_strong=not shop.get("all"))
                if game is None:
                    continue
                variants = p.get("variants") or []
                if not any(v.get("available") for v in variants):
                    continue
                price = next((v.get("price") for v in variants if v.get("available")), "")
                base = url.split("/collections/")[0].split("/products.json")[0]
                prod = Product(retailer="shop", id=f"{shop['key']}:{p.get('id')}", title=title,
                               game=game, url=f"{base}/products/{p.get('handle')}",
                               price=f"${price}" if price else "",
                               image=((p.get("images") or [{}])[0].get("src") or ""))
                hits.append(Hit(product=prod, store=store if local else None,
                                note=f"at {shop['name']}" if not local else "in stock"))
            if len(items) < 250:
                break
            await asyncio.sleep(self.delay)
        return hits

    async def scan(self, trackers: list[dict], shops: list[dict] | None = None, **_) -> list[Hit]:
        shops = shops or []
        hits: list[Hit] = []
        self.errors = {}
        for shop in shops:
            try:
                hits += await self.scan_shop(shop)
            except RuntimeError as e:
                self.errors[shop["name"]] = str(e)
                log.warning("shop %s: %s", shop["name"], e)
            await asyncio.sleep(self.delay)
        ok = len(shops) - len(self.errors)
        self.last_summary = f"{ok}/{len(shops)} shops ok · {len(hits)} in-stock"
        if shops and ok == 0:
            raise RuntimeError("every shop failed: " + "; ".join(self.errors.values()))
        return hits
