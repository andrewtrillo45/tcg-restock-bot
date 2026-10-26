from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Store:
    retailer: str          # "target", "bestbuy", "shop"
    id: str
    name: str
    city: str = ""
    state: str = ""
    zip: str = ""
    address: str = ""
    lat: float | None = None
    lon: float | None = None

    @property
    def key(self) -> str:
        return f"{self.retailer}:{self.id}"

    @property
    def label(self) -> str:
        where = ", ".join(p for p in (self.city, self.state) if p)
        return f"{self.name} ({where})" if where else self.name


@dataclass
class Product:
    retailer: str          # "target", "bestbuy", "shop", "url"
    id: str
    title: str
    game: str              # "pokemon" | "yugioh" | "other"
    url: str = ""
    price: str = ""
    image: str = ""
    release_date: str = ""   # YYYY-MM-DD when the retailer publishes a street date

    @property
    def key(self) -> str:
        return f"{self.retailer}:{self.id}"


@dataclass
class Hit:
    """One product in stock at one place. store=None means online."""
    product: Product
    store: Store | None = None
    qty: int | None = None
    note: str = ""          # "in store", "pickup", "online", …

    @property
    def key(self) -> str:
        return f"{self.product.key}@{self.store.key if self.store else 'online'}"


@dataclass
class Alert:
    kind: str               # "local" | "online"
    product: Product
    hits: list[Hit]
    # (user_id, nearest_miles) — nearest_miles is None for online alerts
    recipients: list[tuple[str, float | None]] = field(default_factory=list)
