"""
agentns.health_checker
======================
Async health checking for agent endpoints.

Probes the endpoint's health URL and returns a normalised dict:

    {
        "status":           "healthy" | "degraded" | "unhealthy",
        "load":             0.0–100.0,          # CPU/load percent
        "response_time_ms": float,              # round-trip ms
        "last_check":       ISO-8601 UTC string,
    }

Discovery order for the health URL
-----------------------------------
1. Explicitly supplied ``health_check_url``
2. ``{endpoint}/.well-known/agent.json``   (A2A AgentCard standard)
3. ``{endpoint}/health``                   (REST convention)

Any 2xx response is "healthy". Slow 2xx (> SLOW_MS) → "degraded".
Connection error / non-2xx → "unhealthy".
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

CONNECT_TIMEOUT = float(5)
READ_TIMEOUT    = float(5)
SLOW_MS         = float(2000)   # response time above which status becomes "degraded"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=5, pool=5),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
    return _client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unhealthy(reason: str = "") -> Dict:
    return {
        "status":           "unhealthy",
        "load":             100.0,
        "response_time_ms": 0.0,
        "last_check":       _now_iso(),
        "reason":           reason,
    }


async def check_agent_health(health_url: str) -> Dict:
    """
    Probe *health_url* and return a normalised health dict.

    Parameters
    ----------
    health_url:
        Full URL to probe (e.g. ``http://host:8080/health``).

    Returns
    -------
    dict with keys: status, load, response_time_ms, last_check
    """
    client = await _get_client()
    t0 = time.perf_counter()
    try:
        resp = await client.get(health_url)
        elapsed = (time.perf_counter() - t0) * 1000  # ms

        if resp.status_code >= 400:
            return _unhealthy(f"HTTP {resp.status_code}")

        # Try to read load from JSON body (standard agentns /health format)
        load = 50.0
        try:
            body = resp.json()
            if isinstance(body, dict):
                load = float(body.get("load_percent", body.get("load", 50.0)))
        except Exception:
            pass

        status = "healthy"
        if load >= 90 or elapsed > SLOW_MS:
            status = "degraded"

        return {
            "status":           status,
            "load":             round(load, 1),
            "response_time_ms": round(elapsed, 1),
            "last_check":       _now_iso(),
        }

    except httpx.ConnectError:
        return _unhealthy("connection refused")
    except httpx.TimeoutException:
        return _unhealthy("timeout")
    except Exception as exc:
        return _unhealthy(str(exc)[:80])


async def probe_endpoint(endpoint: str) -> Dict:
    """
    Try the three standard discovery URLs for *endpoint* and return
    the first successful result.  Falls through to unhealthy if all fail.
    """
    candidates = [
        endpoint.rstrip("/") + "/.well-known/agent.json",
        endpoint.rstrip("/") + "/health",
        endpoint.rstrip("/") + "/healthz",
    ]
    for url in candidates:
        result = await check_agent_health(url)
        if result["status"] != "unhealthy":
            return result
    return _unhealthy("all probe URLs failed")
