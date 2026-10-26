"""ZIP → coordinates (zippopotam.us, free, no key) and distance math."""
from __future__ import annotations

import math
import re

EARTH_RADIUS_MI = 3958.8
_ZIP_RE = re.compile(r"^\d{5}$")
_cache: dict[str, dict | None] = {}


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def valid_zip(z: str) -> bool:
    return bool(_ZIP_RE.match(str(z).strip()))


async def lookup_zip(fetcher, zip_code: str) -> dict | None:
    """Return {"zip","lat","lon","city","state"} or None if unknown."""
    zip_code = str(zip_code).strip()[:5]
    if not valid_zip(zip_code):
        return None
    if zip_code in _cache:
        return _cache[zip_code]
    status, data = await fetcher.get_json(f"https://api.zippopotam.us/us/{zip_code}")
    info = None
    if status == 200 and isinstance(data, dict):
        places = data.get("places") or []
        if places:
            p = places[0]
            try:
                info = {
                    "zip": zip_code,
                    "lat": float(p["latitude"]),
                    "lon": float(p["longitude"]),
                    "city": p.get("place name", ""),
                    "state": p.get("state abbreviation", ""),
                }
            except (KeyError, ValueError):
                info = None
    if info is not None or status == 404:
        _cache[zip_code] = info   # don't cache transient failures
    return info
