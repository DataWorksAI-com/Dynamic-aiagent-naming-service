"""
agentns.geocoder
================
Automatic city-name → (latitude, longitude) resolution.

Resolution order
----------------
1. Built-in CITY_COORDS table (instant, no network)
2. In-process memory cache (instant, no network)
3. OpenStreetMap Nominatim API (free, no API key, works for any city on Earth)
4. Returns None — geo-routing disabled for that endpoint (never raises)

Nominatim fair-use policy
--------------------------
  - Max 1 request per second (enforced by _rate_limiter)
  - User-Agent header required (set to "agentns/2.0.0")
  - Results cached indefinitely in _geocode_cache — a city name never
    changes its coordinates, so cache never needs to expire.

To disable external geocoding entirely (air-gapped environments):
  Set env var AGENTNS_GEOCODING=off
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Optional, Tuple

import httpx

from .server_selection import CITY_COORDS

logger = logging.getLogger("agentns.geocoder")

GEOCODING_ENABLED = os.getenv("AGENTNS_GEOCODING", "on").lower() != "off"
NOMINATIM_URL     = "https://nominatim.openstreetmap.org/search"
NOMINATIM_TIMEOUT = 5.0   # seconds

# In-process cache — city_key (lowercase) → (lat, lon) or None (lookup failed)
_geocode_cache: Dict[str, Optional[Tuple[float, float]]] = {}
_geocode_lock  = asyncio.Lock()

# Rate limiter — Nominatim policy: max 1 request/second
_last_request_time: float = 0.0
_rate_lock = asyncio.Lock()


async def _wait_for_rate_limit() -> None:
    """Enforce 1 req/s to Nominatim."""
    global _last_request_time
    async with _rate_lock:
        now     = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)
        _last_request_time = time.monotonic()


async def _nominatim_lookup(city: str) -> Optional[Tuple[float, float]]:
    """Call Nominatim and return (lat, lon) or None."""
    await _wait_for_rate_limit()
    try:
        async with httpx.AsyncClient(timeout=NOMINATIM_TIMEOUT) as client:
            resp = await client.get(
                NOMINATIM_URL,
                params={"q": city, "format": "json", "limit": 1},
                headers={"User-Agent": "agentns/3.0.0 (https://github.com/tonystark3110/agentns)"},
            )
        if resp.status_code != 200:
            logger.warning(f"Nominatim returned HTTP {resp.status_code} for '{city}'")
            return None
        results = resp.json()
        if not results:
            logger.warning(f"Nominatim found no results for '{city}'")
            return None
        lat = float(results[0]["lat"])
        lon = float(results[0]["lon"])
        logger.info(f"Geocoded '{city}' → ({lat:.4f}, {lon:.4f}) via Nominatim")
        return (lat, lon)
    except Exception as exc:
        logger.warning(f"Nominatim geocoding failed for '{city}': {exc}")
        return None


async def resolve_city(city: str) -> Optional[Tuple[float, float]]:
    """
    Resolve a city name to (latitude, longitude).

    Parameters
    ----------
    city : str
        Any city name, e.g. "Newark", "Hyderabad", "Gdansk".

    Returns
    -------
    (lat, lon) tuple, or None if resolution failed.
    Returning None means geo-routing will be disabled for that endpoint.
    """
    if not city:
        return None

    key = city.lower().strip()

    # 1 — built-in table (instant)
    if key in CITY_COORDS:
        return CITY_COORDS[key]

    # 2 — memory cache (instant)
    async with _geocode_lock:
        if key in _geocode_cache:
            return _geocode_cache[key]

    # 3 — Nominatim (network, ~200–500 ms)
    if not GEOCODING_ENABLED:
        logger.warning(
            f"City '{city}' not in built-in table and AGENTNS_GEOCODING=off — "
            f"geo-routing disabled. Pass explicit latitude/longitude to enable."
        )
        async with _geocode_lock:
            _geocode_cache[key] = None
        return None

    result = await _nominatim_lookup(city)

    # Cache result (even None — avoids hammering Nominatim for unknown cities)
    async with _geocode_lock:
        _geocode_cache[key] = result

    if result is None:
        logger.warning(
            f"Could not geocode '{city}' — geo-routing disabled for this endpoint. "
            f"Pass explicit latitude/longitude in the registration payload to override."
        )

    return result


def geocode_cache_snapshot() -> Dict[str, Optional[Tuple[float, float]]]:
    """Return a copy of the current geocode cache (for /health or debugging)."""
    return dict(_geocode_cache)
