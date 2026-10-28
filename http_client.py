"""Tiny HTTP adapter so the checkers can be tested without a network.

Production uses AiohttpFetcher. Tests pass in a fake with the same two methods.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("http")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.target.com",
    "Referer": "https://www.target.com/",
}


import os
from urllib.parse import urlsplit

# Optional: route the bot-walled sites (Walmart, GameStop, Hot Topic, FYE) through a residential proxy.
# PROXY_URL=http://user:pass@host:port   PROXY_SITES=walmart.com,gamestop.com (default: those four)
PROXY_URL = os.getenv("PROXY_URL", "").strip()
PROXY_SITES = [x.strip().lower() for x in os.getenv(
    "PROXY_SITES", "target.com,walmart.com,gamestop.com,hottopic.com,fye.com").split(",") if x.strip()]


# Sites with bot walls (Akamai / PerimeterX) fingerprint the TLS handshake. curl_cffi can impersonate
# a real Chrome handshake, which is what gets a 435/403 turned back into a 200. Used when installed.
IMPERSONATE_SITES = [x.strip().lower() for x in os.getenv(
    "IMPERSONATE_SITES", "target.com,walmart.com,gamestop.com,hottopic.com,fye.com").split(",") if x.strip()]
IMPERSONATE = os.getenv("IMPERSONATE", "chrome")
try:
    from curl_cffi.requests import AsyncSession as _CurlSession
except Exception:  # not installed → plain aiohttp
    _CurlSession = None


def impersonate_for(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return _CurlSession is not None and any(host.endswith(s) for s in IMPERSONATE_SITES)


def proxy_for(url: str) -> str | None:
    if not PROXY_URL:
        return None
    host = urlsplit(url).netloc.lower()
    return PROXY_URL if any(host.endswith(s) for s in PROXY_SITES) else None


class AiohttpFetcher:
    def __init__(self, timeout_s: float = 20.0):
        import aiohttp  # imported lazily so tests can run without it

        self._aiohttp = aiohttp
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: Any = None

    async def start(self):
        if self._session is None:
            self._session = self._aiohttp.ClientSession(timeout=self._timeout)
        if _CurlSession is not None and getattr(self, "_curl", None) is None:
            self._curl = _CurlSession(impersonate=IMPERSONATE, timeout=self._timeout.total or 20)

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None
        if getattr(self, "_curl", None) is not None:
            try:
                await self._curl.close()
            except Exception:
                pass
            self._curl = None

    async def _curl_get(self, url, params, headers, want_json: bool):
        """GET through curl_cffi (Chrome TLS fingerprint). Same return shapes as get_json / get_text."""
        try:
            r = await self._curl.get(url, params=params, headers=headers, proxy=proxy_for(url), allow_redirects=True)
            if want_json:
                try:
                    return r.status_code, r.json()
                except Exception:
                    return r.status_code, None
            return r.status_code, r.text, str(r.url)
        except Exception as e:
            log.debug("curl GET %s failed: %s", url, e)
            return (0, None) if want_json else (0, "", url)

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        """Return (status, json_or_None)."""
        await self.start()
        hdrs = {**BROWSER_HEADERS, **(headers or {})}
        if impersonate_for(url):
            return await self._curl_get(url, params, hdrs, True)
        try:
            async with self._session.get(url, params=params, headers=hdrs, proxy=proxy_for(url)) as r:
                status = r.status
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = None
                return status, data
        except asyncio.TimeoutError:
            return 0, None
        except Exception as e:  # DNS, TLS, connection reset …
            log.debug("GET %s failed: %s", url, e)
            return 0, None

    async def post_json(self, url: str, body: dict, headers: dict | None = None):
        """POST JSON. Return (status, json_or_None)."""
        await self.start()
        hdrs = {"User-Agent": "tcg-restock-bot/1.0", **(headers or {})}
        try:
            async with self._session.post(url, json=body, headers=hdrs) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = None
                return r.status, data
        except asyncio.TimeoutError:
            return 0, None
        except Exception as e:
            log.debug("POST %s failed: %s", url, e)
            return 0, None

    async def get_text(self, url: str, params: dict | None = None, headers: dict | None = None):
        """Return (status, text_or_empty, final_url)."""
        await self.start()
        hdrs = {**BROWSER_HEADERS, "Accept": "text/html,*/*", **(headers or {})}
        if impersonate_for(url):
            return await self._curl_get(url, params, hdrs, False)
        try:
            async with self._session.get(url, params=params, headers=hdrs, allow_redirects=True,
                                         proxy=proxy_for(url)) as r:
                text = await r.text(errors="ignore")
                return r.status, text, str(r.url)
        except asyncio.TimeoutError:
            return 0, "", url
        except Exception as e:
            log.debug("GET %s failed: %s", url, e)
            return 0, "", url
