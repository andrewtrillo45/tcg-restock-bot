"""Target — in-store / pickup stock near each tracked ZIP via Target's public RedSky API.

Endpoints used (all return JSON; the web key is the one target.com itself uses):
  nearby_stores_v1                       → Target stores within N miles of a ZIP
  plp_search_v2                          → product search ("pokemon trading cards")
  product_summary_with_fulfillment_v1    → per-store availability for up to ~25 TCINs
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

import filters
from geo import lookup_zip
from models import Hit, Product, Store

log = logging.getLogger("target")

REDSKY = "https://redsky.target.com/redsky_aggregations/v1/web"
DEFAULT_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"
IN_STOCK = {"IN_STOCK", "LIMITED_STOCK"}


def _dig(d, *path, default=None):
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and isinstance(p, int) and p < len(cur):
            cur = cur[p]
        else:
            return default
        if cur is None:
            return default
    return cur


class TargetChecker:
    name = "target"
    label = "Target"

    def __init__(self, fetcher, key: str | None = None, max_stores: int = 20,
                 search_pages: int = 2, delay: float = 0.35, store_cache_hours: int = 24,
                 product_cache_hours: int = 6):
        self.fetcher = fetcher
        self.key = key or DEFAULT_KEY
        self.max_stores = max_stores
        self.search_pages = search_pages
        self.delay = delay
        self.visitor_id = uuid.uuid4().hex.upper()
        self._store_cache: dict[tuple[str, int], tuple[float, list[Store]]] = {}
        self._store_ttl = store_cache_hours * 3600
        self._product_cache: dict[str, tuple[float, list[Product]]] = {}
        self._product_ttl = product_cache_hours * 3600
        self._pinned_cache: dict[str, tuple[float, Product | None]] = {}
        self.known_products: dict[str, Product] = {}   # tcin → Product, refreshed each scan
        self.last_summary = ""
        self.calls = 0

    # ---------- stores ----------
    async def nearby_stores(self, zip_code: str, radius: int) -> list[Store]:
        ck = (zip_code, radius)
        cached = self._store_cache.get(ck)
        if cached and time.time() - cached[0] < self._store_ttl:
            return cached[1]
        params = {"key": self.key, "limit": self.max_stores, "within": radius, "place": zip_code}
        stores: list[Store] = []
        for endpoint in ("nearby_stores_v1", "store_locator_v1"):
            self.calls += 1
            status, data = await self.fetcher.get_json(f"{REDSKY}/{endpoint}", params=params)
            raw = (_dig(data, "data", "nearby_stores", "stores")
                   or _dig(data, "data", "store_locator", "stores")
                   or _dig(data, "data", "stores") or [])
            if status == 200 and raw:
                for s in raw:
                    st = self._parse_store(s)
                    if st:
                        stores.append(st)
                break
            log.warning("Target %s returned %s for %s", endpoint, status, zip_code)
        # fill in missing coordinates from the store's own ZIP
        for st in stores:
            if (st.lat is None or st.lon is None) and st.zip:
                info = await lookup_zip(self.fetcher, st.zip)
                if info:
                    st.lat, st.lon = info["lat"], info["lon"]
        stores = [s for s in stores if s.lat is not None][: self.max_stores]
        if stores:
            self._store_cache[ck] = (time.time(), stores)
        return stores

    @staticmethod
    def _parse_store(s: dict) -> Store | None:
        sid = str(s.get("store_id") or s.get("location_id") or "")
        if not sid:
            return None
        addr = s.get("mailing_address") or s.get("address") or {}
        geo = s.get("geographic_specifications") or s.get("geographic_specification") or {}
        lat = geo.get("latitude", s.get("latitude"))
        lon = geo.get("longitude", s.get("longitude"))
        try:
            lat = float(lat) if lat is not None else None
            lon = float(lon) if lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        return Store(
            retailer="target", id=sid,
            name=f"Target {s.get('location_name') or s.get('store_name') or sid}",
            city=addr.get("city", ""), state=addr.get("region", "") or addr.get("state", ""),
            zip=str(addr.get("postal_code", "") or "")[:5],
            address=addr.get("address_line1", ""), lat=lat, lon=lon,
        )

    # ---------- products ----------
    async def search(self, keyword: str, zip_info: dict, store_id: str, game: str) -> list[Product]:
        cached = self._product_cache.get(keyword)
        if cached and time.time() - cached[0] < self._product_ttl:
            return cached[1]
        found: dict[str, Product] = {}
        count = 24
        for page in range(self.search_pages):
            params = {
                "key": self.key, "channel": "WEB", "count": count, "offset": page * count,
                "keyword": keyword, "page": f"/s/{keyword}", "platform": "desktop",
                "pricing_store_id": store_id, "visitor_id": self.visitor_id,
                "zip": zip_info["zip"], "state": zip_info["state"],
                "latitude": zip_info["lat"], "longitude": zip_info["lon"],
                "useragent": "Mozilla/5.0", "scheduled_delivery_store_id": store_id,
            }
            self.calls += 1
            status, data = await self.fetcher.get_json(f"{REDSKY}/plp_search_v2", params=params)
            if status != 200 or not data:
                log.warning("Target search '%s' page %s returned %s", keyword, page, status)
                break
            items = _dig(data, "data", "search", "products") or []
            if not items:
                break
            for raw in items:
                for p in self._flatten(raw):
                    prod = self._parse_product(p, game)
                    if prod:
                        found[prod.id] = prod
            await asyncio.sleep(self.delay)
        products = list(found.values())
        if products:
            self._product_cache[keyword] = (time.time(), products)
        return products

    @staticmethod
    def _flatten(raw: dict):
        kids = raw.get("children")
        if kids:
            for k in kids:
                yield k
        else:
            yield raw

    @staticmethod
    def _street_date(p: dict) -> str:
        for path in (("item", "mmbv_content", "street_date"), ("item", "street_date"),
                     ("street_date",), ("item", "product_description", "street_date")):
            v = _dig(p, *path)
            if isinstance(v, str) and len(v) >= 10:
                return v[:10]
        return ""

    @staticmethod
    def _parse_product(p: dict, game: str | None, strict: bool = True) -> Product | None:
        tcin = str(p.get("tcin") or "")
        title = _dig(p, "item", "product_description", "title") or p.get("title") or ""
        if not tcin or not title:
            return None
        if _dig(p, "item", "fulfillment", "is_marketplace"):
            return None  # Target Plus third-party seller — never in a store
        if strict:
            if filters.match(title, {game} if game else None) is None:
                return None
        game = game or filters.which_game(title) or "other"
        price = (_dig(p, "price", "formatted_current_price")
                 or _dig(p, "price", "current_retail") or "")
        return Product(
            retailer="target", id=tcin, title=title, game=game,
            url=_dig(p, "item", "enrichment", "buy_url") or f"https://www.target.com/p/-/A-{tcin}",
            price=str(price), image=_dig(p, "item", "enrichment", "images", "primary_image_url") or "",
            release_date=TargetChecker._street_date(p),
        )

    async def product_details(self, tcin: str, store_id: str, zip_info: dict) -> Product | None:
        """Look one TCIN up directly (for products pinned with /addsku)."""
        params = {"key": self.key, "tcin": tcin, "pricing_store_id": store_id, "store_id": store_id,
                  "zip": zip_info["zip"], "state": zip_info["state"], "visitor_id": self.visitor_id,
                  "has_pricing_store_id": "true", "has_financing_options": "false", "channel": "WEB"}
        self.calls += 1
        status, data = await self.fetcher.get_json(f"{REDSKY}/pdp_client_v1", params=params)
        p = _dig(data, "data", "product") if status == 200 else None
        if not p:
            return None
        return self._parse_product(p, None, strict=False)

    # ---------- availability ----------
    async def fulfillment(self, tcins: list[str], store: Store, zip_info: dict) -> dict[str, dict]:
        """Return {tcin: {"in_store": bool, "pickup": bool, "qty": int|None, "online": bool}}."""
        params = {
            "key": self.key, "tcins": ",".join(tcins), "store_id": store.id,
            "zip": zip_info["zip"], "state": zip_info["state"],
            "latitude": zip_info["lat"], "longitude": zip_info["lon"],
            "scheduled_delivery_store_id": store.id, "required_store_id": store.id,
            "has_required_store_id": "true", "channel": "WEB", "page": "/s/pokemon",
        }
        self.calls += 1
        status, data = await self.fetcher.get_json(
            f"{REDSKY}/product_summary_with_fulfillment_v1", params=params)
        if status != 200 or not data:
            raise RuntimeError(f"fulfillment returned HTTP {status} (key rotated or blocked?)")
        out: dict[str, dict] = {}
        for summ in _dig(data, "data", "product_summaries") or []:
            tcin = str(summ.get("tcin") or "")
            ful = summ.get("fulfillment") or {}
            rec = {"in_store": False, "pickup": False, "qty": None,
                   "online": (_dig(ful, "shipping_options", "availability_status") in IN_STOCK)}
            for opt in ful.get("store_options") or []:
                if str(opt.get("location_id") or "") not in ("", store.id):
                    continue
                in_store = _dig(opt, "in_store_only", "availability_status")
                pickup = _dig(opt, "order_pickup", "availability_status")
                rec["in_store"] = rec["in_store"] or in_store in IN_STOCK
                rec["pickup"] = rec["pickup"] or pickup in IN_STOCK
                q = opt.get("location_available_to_promise_quantity")
                if isinstance(q, (int, float)):
                    rec["qty"] = int(q)
            out[tcin] = rec
        return out

    # ---------- the scan ----------
    async def scan(self, trackers: list[dict], skus: list[dict] | None = None, **_) -> list[Hit]:
        trackers = [t for t in trackers if t.get("lat") is not None]
        if not trackers:
            self.last_summary = "no one is tracking yet"
            return []
        pinned = [s["id"] for s in (skus or []) if s.get("retailer") == "target"]
        games = set()
        for t in trackers:
            games.update(t.get("games") or [])
        self.calls = 0

        # 1. union of stores inside every tracker's circle
        stores: dict[str, Store] = {}
        for t in trackers:
            for s in await self.nearby_stores(t["zip"], int(t.get("radius") or 50)):
                stores.setdefault(s.id, s)
        if not stores:
            raise RuntimeError("could not load any Target stores (store lookup failed)")
        first = trackers[0]
        zip_info = {"zip": first["zip"], "state": first.get("state", ""),
                    "lat": first["lat"], "lon": first["lon"]}
        anchor = next(iter(stores.values()))

        # 2. product list per game
        products: dict[str, Product] = {}
        for game in games:
            for kw in filters.GAMES[game]["search"]["target"]:
                for p in await self.search(kw, zip_info, anchor.id, game):
                    products.setdefault(p.id, p)
        # pinned TCINs (added with /addsku) — looked up directly, cached for a few hours
        for tcin in pinned:
            if tcin in products:
                continue
            cached = self._pinned_cache.get(tcin)
            if cached and time.time() - cached[0] < self._product_ttl:
                prod = cached[1]
            else:
                prod = await self.product_details(tcin, anchor.id, zip_info)
                self._pinned_cache[tcin] = (time.time(), prod)
                await asyncio.sleep(self.delay)
            if prod:
                products[tcin] = prod
            else:
                products[tcin] = Product(retailer="target", id=tcin, title=f"Target item {tcin}",
                                         game="other", url=f"https://www.target.com/p/-/A-{tcin}")
        if not products:
            raise RuntimeError("product search returned nothing (blocked or key rotated?)")
        self.known_products = dict(products)

        # 3. availability, store by store
        hits: list[Hit] = []
        online_seen: set[str] = set()
        tcins = list(products)
        chunks = [tcins[i:i + 20] for i in range(0, len(tcins), 20)]
        ok_calls = failed_calls = 0
        last_err = ""
        for store in stores.values():
            for chunk in chunks:
                try:
                    recs = await self.fulfillment(chunk, store, zip_info)
                    ok_calls += 1
                except RuntimeError as e:
                    failed_calls += 1
                    last_err = str(e)
                    await asyncio.sleep(self.delay)
                    continue
                for tcin, rec in recs.items():
                    prod = products.get(tcin)
                    if not prod:
                        continue
                    if rec["in_store"] or rec["pickup"]:
                        note = "in store" if rec["in_store"] else "order pickup"
                        hits.append(Hit(product=prod, store=store, qty=rec["qty"], note=note))
                    if rec["online"] and tcin not in online_seen:
                        online_seen.add(tcin)
                        hits.append(Hit(product=prod, store=None, note="online (ships)"))
                await asyncio.sleep(self.delay)
        if ok_calls == 0 and failed_calls:
            raise RuntimeError(f"every availability call failed — {last_err}")
        self.last_summary = (f"{len(stores)} stores · {len(products)} products · "
                             f"{len(hits)} in-stock · {self.calls} calls"
                             + (f" · {failed_calls} failed" if failed_calls else ""))
        return hits
