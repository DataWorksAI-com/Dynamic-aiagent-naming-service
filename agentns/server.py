"""
agentns.server
==============
Single-binary Agent Name Service sidecar with built-in switchboard federation.

Combines all three resolution hops into one process:

    Recursive Resolver  →  Namespace Registry  →  Authoritative NS

Any agent framework in any language calls the HTTP API:

    POST   /resolve              { "agent_name": "urn:myco.com:sales:emailer" }
    POST   /register             { "label": "emailer", "endpoint": "http://..." }
    DELETE /register/{label}     remove an endpoint
    ANY    /proxy/{label}[/path] forward requests to the best healthy endpoint
    GET    /health
    GET    /agents
    POST   /cache/clear
    GET    /cache/stats

    GET    /switchboard/registries           list this + all connected registries
    POST   /switchboard/registries           connect a remote registry  {tld, url}
    DELETE /switchboard/registries/{tld}     disconnect a remote registry

Multi-registry federation (Switchboard)
----------------------------------------
Each agentns instance owns a TLD (AGENTNS_TLD). When a /resolve request arrives
for a URN whose TLD belongs to a different registry, agentns looks up the
federation map and forwards the request to the correct remote instance.

    Registry A  (TLD=payments.acme.io)    Registry B  (TLD=alerts.acme.io)
         ↑                                      ↑
         └──── both registered in ─────────────┘
                    Switchboard registry
                    (knows TLD→URL mapping)

    POST /resolve {"agent_name": "urn:alerts.acme.io:prod:emailer"}
         → Switchboard sees TLD=alerts.acme.io → routes to Registry B → returns result

Configuration (environment variables — zero hardcoded values)
-------------------------------------------------------------
    AGENTNS_PORT              HTTP port            (default: 8200)
    AGENTNS_NAMESPACE         Default URN namespace (default: "agents.local")
    AGENTNS_TLD               URN TLD this instance owns (default: "agentns.local")
    AGENTNS_HEALTH_INTERVAL   Background health sweep interval in s  (default: 30)
    MONGODB_URI               MongoDB connection string (optional; in-memory if absent)
    MONGODB_DB                MongoDB database name    (default: "agentns")
    FEDERATION_REGISTRIES     Remote registries to connect at startup.
                              JSON:  '{"payments.io":"http://pay:8200","alerts.io":"http://a:8200"}'
                              CSV:   payments.io=http://pay:8200,alerts.io=http://a:8200
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response, StreamingResponse

from .auth             import security_headers_middleware
from .cache            import ResolutionCache
from .geocoder         import resolve_city, geocode_cache_snapshot
from .health_checker   import check_agent_health, probe_endpoint
from .server_selection import rank_servers, select_protocol, calculate_ttl
from .urn_parser       import parse_urn, build_urn

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [agentns] %(message)s",
)
logger = logging.getLogger("agentns")

# ── rate limiting (optional — requires slowapi) ────────────────────────────────
# Import early so _limit() is available as a decorator on route functions.
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address
    _limiter = Limiter(key_func=get_remote_address)
    _RATE_LIMIT_AVAILABLE = True
except ImportError:
    _limiter = None
    _RATE_LIMIT_AVAILABLE = False


def _limit(rate: str):
    """Return a slowapi rate-limit decorator if slowapi is installed, otherwise a no-op."""
    if _limiter is not None:
        return _limiter.limit(rate)
    return lambda f: f

# ── config from env ────────────────────────────────────────────────────────────
PORT             = int(os.getenv("AGENTNS_PORT",            "8200"))
DEFAULT_NS       = os.getenv("AGENTNS_NAMESPACE",           "agents.local")
DEFAULT_TLD      = os.getenv("AGENTNS_TLD",                 "agentns.local")
HEALTH_INTERVAL  = int(os.getenv("AGENTNS_HEALTH_INTERVAL", "30"))
MONGODB_URI      = os.getenv("MONGODB_URI",                 "")
MONGODB_DB       = os.getenv("MONGODB_DB",                  "agentns")

# ── A2A Proxy config (optional — when set, /resolve returns proxy URL) ─────────
#
# Two ways to configure — use whichever fits your setup:
#
# Option A — High-level (recommended for Agentgateway):
#   AGENTNS_PROXY_HOST=agentgateway       hostname or IP of the proxy
#   AGENTNS_PROXY_PORT=8400               port (default: 8400)
#   AGENTNS_PROXY_MODE=agentgateway       "agentgateway" (default) | "custom"
#   SLIM_ORG=my-org                       optional SLIM org prefix
#
# Option B — Low-level (full manual control):
#   A2A_PROXY_ENDPOINTS=http://proxy:8400 comma-separated proxy base URLs
#   SLIM_ORG=my-org
#
# When either option is set, /resolve returns:
#   url:           "http://agentgateway:8400/a2a/my-namespace/alerts"
#   via_proxy:     true
#   slim_identity: "my-org/my-namespace/alerts"
#
# Agentgateway URL format:   {proxy_base}/a2a/{namespace}/{label}
# Agentgateway docs:         https://agentgateway.dev/docs/guides/a2a-proxy

_PROXY_HOST = os.getenv("AGENTNS_PROXY_HOST", "").strip()
_PROXY_PORT = os.getenv("AGENTNS_PROXY_PORT", "8400").strip()
_PROXY_MODE = os.getenv("AGENTNS_PROXY_MODE", "agentgateway").lower().strip()
SLIM_ORG    = os.getenv("SLIM_ORG", "")

# Build the proxy endpoints list — Option B (explicit) takes precedence over Option A (derived)
_raw_proxy_eps = os.getenv("A2A_PROXY_ENDPOINTS", "")
if _raw_proxy_eps:
    _PROXY_ENDPOINTS: List[str] = [ep.strip() for ep in _raw_proxy_eps.split(",") if ep.strip()]
elif _PROXY_HOST:
    _proxy_scheme = "https" if _PROXY_PORT in ("443", "8443") else "http"
    _PROXY_ENDPOINTS = [f"{_proxy_scheme}://{_PROXY_HOST}:{_PROXY_PORT}"]
else:
    _PROXY_ENDPOINTS = []

_start_time = _time.time()

# ── Federation / Switchboard ───────────────────────────────────────────────────
# Maps TLD → {url, registry_id, status, added_at}
# Each entry is a remote agentns instance that owns agents under that TLD.
# Populated from FEDERATION_REGISTRIES env var at startup, and managed at
# runtime via POST/DELETE /switchboard/registries.
_federation: Dict[str, Dict] = {}


def _load_federation_from_env() -> None:
    """Parse FEDERATION_REGISTRIES env var into _federation at startup."""
    raw = os.getenv("FEDERATION_REGISTRIES", "").strip()
    if not raw:
        return
    entries: Dict[str, str] = {}
    try:
        entries = json.loads(raw)                       # JSON format preferred
    except json.JSONDecodeError:
        for pair in raw.split(","):                     # CSV fallback: tld=url,...
            pair = pair.strip()
            if "=" in pair:
                tld, url = pair.split("=", 1)
                entries[tld.strip()] = url.strip()
    for tld, url in entries.items():
        _federation[tld.strip()] = {
            "url":         url.strip().rstrip("/"),
            "registry_id": tld.strip(),
            "status":      "configured",
            "added_at":    "startup",
        }
    if _federation:
        logger.info(f"Federation: {len(_federation)} remote registr(ies) loaded: {list(_federation)}")


# Load federation at import time (sync — just dict operations)
_load_federation_from_env()

# ── in-memory registry ─────────────────────────────────────────────────────────
# { label -> [endpoint_dict, ...] }
_registry: Dict[str, List[Dict]] = {}

# ── shared HTTP client for the built-in proxy ──────────────────────────────────
# Created once in lifespan; reuses TCP connections across requests.
_proxy_client: Optional[httpx.AsyncClient] = None

# { http_endpoint -> health_dict }
_health_cache: Dict[str, Dict] = {}
_health_lock = asyncio.Lock()

# Resolution cache (TTL-based)
_cache = ResolutionCache()

# MongoDB collection handle (None if not configured)
_mongo_col = None


# ── MongoDB ────────────────────────────────────────────────────────────────────

async def _init_mongo() -> None:
    global _mongo_col
    if not MONGODB_URI:
        logger.warning("MONGODB_URI not set — registry is in-memory only (lost on restart)")
        return
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(MONGODB_URI, serverSelectionTimeoutMS=6000)
        db     = client[MONGODB_DB]
        _mongo_col = db["agents"]
        await _mongo_col.create_index("label")
        await _mongo_col.create_index([("label", 1), ("endpoint", 1)], unique=True)
        await client.admin.command("ping")
        logger.info(f"MongoDB connected: {MONGODB_DB}.agents")
    except Exception as exc:
        logger.error(f"MongoDB connection failed ({exc}) — running without persistence")
        _mongo_col = None


async def _load_from_mongo() -> None:
    if _mongo_col is None:
        return
    count = 0
    try:
        async for doc in _mongo_col.find({}):
            label = doc["label"]
            ep    = {k: v for k, v in doc.items() if k not in ("_id", "label")}
            existing = [e["endpoint"] for e in _registry.get(label, [])]
            if ep.get("endpoint") and ep["endpoint"] not in existing:
                _registry.setdefault(label, []).append(ep)
                count += 1
        logger.info(f"Loaded {count} agent endpoint(s) from MongoDB")
    except Exception as exc:
        logger.error(f"MongoDB load failed: {exc}")


async def _save_to_mongo(label: str, entry: Dict) -> None:
    if _mongo_col is None:
        return
    try:
        doc = dict(entry)
        doc["label"] = label
        now = datetime.now(timezone.utc)
        await _mongo_col.update_one(
            {"label": label, "endpoint": entry["endpoint"]},
            {
                "$set":         {**doc, "last_seen": now},
                "$setOnInsert": {"registered_at": now},
            },
            upsert=True,
        )
    except Exception as exc:
        logger.error(f"MongoDB save failed ({label}/{entry['endpoint']}): {exc}")


# ── background health loop ─────────────────────────────────────────────────────

async def _check_all() -> None:
    seen: Dict[str, str] = {}
    for eps in _registry.values():
        for ep in eps:
            url = ep["endpoint"]
            if url not in seen:
                seen[url] = ep.get("health_check_url", "")

    if not seen:
        return

    async def _one(endpoint_url: str, hc_url: str) -> None:
        probe_url = hc_url or endpoint_url
        result    = await check_agent_health(probe_url) if hc_url else await probe_endpoint(endpoint_url)
        async with _health_lock:
            _health_cache[endpoint_url] = result

    await asyncio.gather(*[_one(u, h) for u, h in seen.items()], return_exceptions=True)
    logger.debug(f"Health sweep: {len(seen)} endpoint(s) checked")


async def _health_loop() -> None:
    logger.info(f"Background health loop started (interval={HEALTH_INTERVAL}s)")
    while True:
        try:
            await _check_all()
            await _cache.purge_expired()
        except Exception as exc:
            logger.warning(f"Health loop error: {exc}")
        await asyncio.sleep(HEALTH_INTERVAL)


def _cached_health(endpoint_url: str) -> Dict:
    return _health_cache.get(endpoint_url, {
        "status":           "unknown",
        "load":             50.0,
        "response_time_ms": 9999.0,
        "last_check":       None,
    })


async def _check_single(endpoint_url: str, hc_url: str) -> None:
    result = await check_agent_health(hc_url) if hc_url else await probe_endpoint(endpoint_url)
    async with _health_lock:
        _health_cache[endpoint_url] = result


# ── FastAPI lifespan ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    global _proxy_client
    # Shared proxy client — created once, reused across all /proxy/* requests.
    # Separate from the health-checker client (different timeout profile).
    _proxy_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        follow_redirects=False,   # never follow redirects from upstream agents
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )

    await _init_mongo()
    await _load_from_mongo()
    await _check_all()          # initial sweep so first /resolve has real data
    task = asyncio.create_task(_health_loop())

    total = sum(len(v) for v in _registry.values())
    logger.info(f"agentns ready — {total} endpoint(s) across {len(_registry)} label(s) | port {PORT}")
    if _PROXY_ENDPOINTS:
        logger.info(f"A2A proxy enabled — mode={_PROXY_MODE} endpoint={_PROXY_ENDPOINTS[0]}")
    if _federation:
        logger.info(f"Switchboard active — {len(_federation)} remote registr(ies): {list(_federation)}")

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await _proxy_client.aclose()


app = FastAPI(
    title="agentns — Agent Name Service",
    description=(
        "Single-binary service discovery sidecar for multi-agent systems.\n\n"
        "Register agents with POST /register. Resolve them with POST /resolve using "
        "standard URNs (urn:tld:namespace:label). Language-agnostic HTTP API.\n\n"
        "No authentication required — designed for sidecar/internal network deployments. "
        "For public deployments, place behind a reverse proxy that handles auth at the edge."
    ),
    version="3.0.0",
    lifespan=lifespan,
)

# ── Security headers middleware ────────────────────────────────────────────────
app.middleware("http")(security_headers_middleware)

# ── Wire slowapi into app (noop if slowapi not installed) ─────────────────────
if _RATE_LIMIT_AVAILABLE:
    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    logger.info("Rate limiting enabled (slowapi)")
else:
    logger.warning("slowapi not installed — rate limiting disabled. pip install agentns[server]")


# ── Proxy helpers ─────────────────────────────────────────────────────────────

def _build_proxy_response(result: Dict, label: str, namespace: str) -> Dict:
    """
    Enrich a resolve result with proxy URL and SLIM identity when A2A_PROXY_ENDPOINTS is set.

    Before (no proxy):
        result["endpoint"] = "http://agent-host:9001"

    After (with proxy):
        result["url"]           = "http://proxy:8400/a2a/my-namespace/alerts"
        result["endpoint"]      = "http://proxy:8400/a2a/my-namespace/alerts"  ← compat alias
        result["via_proxy"]     = True
        result["slim_identity"] = "my-org/my-namespace/alerts"
        result["metadata"]["direct_endpoint"] = "http://agent-host:9001"       ← preserved
    """
    proxy_base = _PROXY_ENDPOINTS[0] if _PROXY_ENDPOINTS else None
    if not proxy_base:
        result["via_proxy"]     = False
        result["slim_identity"] = ""
        result["url"]           = result.get("endpoint", "")
        return result

    direct_endpoint = result.get("endpoint", "")
    proxy_url       = f"{proxy_base}/a2a/{namespace}/{label}"
    slim_id         = f"{SLIM_ORG}/{namespace}/{label}" if SLIM_ORG else f"{namespace}/{label}"

    result["url"]           = proxy_url
    result["endpoint"]      = proxy_url      # backward-compat alias
    result["via_proxy"]     = True
    result["slim_identity"] = slim_id
    result.setdefault("metadata", {})["direct_endpoint"] = direct_endpoint
    return result


# ── Federation helper ─────────────────────────────────────────────────────────

async def _federated_resolve(remote_url: str, body: Dict) -> Dict:
    """
    Forward a /resolve request to a remote agentns instance.

    The original body is passed through unchanged so requester_context
    (location, protocols) is preserved end-to-end.
    """
    try:
        resp = await _proxy_client.post(
            f"{remote_url}/resolve",
            json=body,
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0),
        )
        if resp.status_code == 200:
            result = resp.json()
            result["federated_from"] = remote_url   # tag so caller knows it came from federation
            return result
        # Propagate remote errors (404 = not found there either, etc.)
        detail = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
        raise HTTPException(status_code=resp.status_code, detail=f"[{remote_url}] {detail}")
    except HTTPException:
        raise
    except httpx.ConnectError:
        raise HTTPException(502, f"Could not reach remote registry at {remote_url}")
    except httpx.TimeoutException:
        raise HTTPException(504, f"Remote registry at {remote_url} timed out")
    except Exception as exc:
        raise HTTPException(502, f"Federation error forwarding to {remote_url}: {exc}")


# ── POST /resolve ──────────────────────────────────────────────────────────────

@app.post("/resolve")
@_limit("60/minute")
async def resolve(request: Request, body: dict):
    """
    Resolve an agent by URN or label.

    Accepts any of:
        { "agent_name": "urn:myco.com:sales:emailer" }
        { "agent":      "emailer",  "namespace": "myco.com:sales" }
        { "label":      "emailer" }

    Optional:
        { "requester_context": { "location": {"city": "Boston"}, "protocols": ["A2A"] } }
        { "cache_enabled": false }
    """
    # ── parse identifier ──────────────────────────────────────────────────────
    agent_name        = (body.get("agent_name") or body.get("urn") or "").strip()
    label_direct      = (body.get("agent") or body.get("label") or "").strip()
    requester_context = body.get("requester_context") or {}
    cache_enabled     = body.get("cache_enabled", True)

    if agent_name:
        parsed = parse_urn(agent_name)

        # ── TLD routing ───────────────────────────────────────────────────────
        # If the URN's TLD doesn't match this instance, route via federation.
        # Multiple namespaces are allowed within one instance — no namespace check.
        #
        #   urn:this-tld:any-namespace:label  → resolve locally (any namespace OK)
        #   urn:other-tld:ns:label            → forward to the registry that owns other-tld
        #   label  (no TLD)                   → resolve locally
        if parsed.tld and parsed.tld != DEFAULT_TLD:
            remote = _federation.get(parsed.tld)
            if remote:
                logger.info(
                    f"Federation: routing urn:{parsed.tld}:... → {remote['url']}"
                )
                return await _federated_resolve(remote["url"], body)
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No registry is registered for TLD '{parsed.tld}'. "
                    f"This instance owns '{DEFAULT_TLD}'. "
                    f"Add the remote registry via POST /switchboard/registries."
                ),
            )

        label = parsed.label

    elif label_direct:
        label = label_direct
    else:
        raise HTTPException(status_code=400, detail="Provide 'agent_name' (URN) or 'label'")

    if not label:
        raise HTTPException(status_code=400, detail="Could not extract agent label from input")

    # ── cache check ───────────────────────────────────────────────────────────
    t0        = _time.monotonic()
    cache_key = _cache.make_key(label, requester_context)

    if cache_enabled:
        cached = await _cache.get(cache_key)
        if cached:
            elapsed = round((_time.monotonic() - t0) * 1000, 1)
            cached["resolution_time_ms"] = elapsed
            cached["cached"] = True
            return cached

    # ── lookup ────────────────────────────────────────────────────────────────
    endpoints = _registry.get(label)
    if not endpoints:
        raise HTTPException(status_code=404, detail=f"No endpoints registered for label '{label}'")

    preferred_protocols = requester_context.get("protocols", [])

    servers = [
        {
            "server_id":        ep["endpoint"],
            "endpoint":         ep["endpoint"],
            "health_check_url": ep.get("health_check_url", ""),
            "protocols":        ep.get("protocols", []),
            "region":           ep.get("region", ""),
            "region_label":     ep.get("region_label", ep.get("region", "")),
            "flag":             ep.get("flag", ""),
            "location":         ep.get("location", {}),
        }
        for ep in endpoints
    ]

    health_map = {s["server_id"]: _cached_health(s["server_id"]) for s in servers}

    # Live-check any endpoint not yet in cache
    unchecked = [s for s in servers if health_map[s["server_id"]]["status"] == "unknown"]
    if unchecked:
        async def _live(s: Dict) -> None:
            result = await check_agent_health(s["health_check_url"]) if s["health_check_url"] \
                else await probe_endpoint(s["endpoint"])
            async with _health_lock:
                _health_cache[s["server_id"]] = result
            health_map[s["server_id"]] = result
        await asyncio.gather(*[_live(s) for s in unchecked], return_exceptions=True)

    # ── rank ──────────────────────────────────────────────────────────────────
    ranked = rank_servers(servers, health_map, requester_context)

    all_candidates = sorted(
        [
            {
                "endpoint":   s["endpoint"],
                "region":     s["region_label"] or s["region"],
                "flag":       s.get("flag", ""),
                "status":     health_map[s["server_id"]].get("status", "unknown"),
                "latency_ms": round(health_map[s["server_id"]].get("response_time_ms", 0.0), 1),
                "load":       health_map[s["server_id"]].get("load", 50.0),
            }
            for s in servers
        ],
        key=lambda c: (c["status"] == "unhealthy", c["latency_ms"]),
    )

    # ── emergency fallback ────────────────────────────────────────────────────
    if not ranked:
        ep        = servers[0]
        protocol  = select_protocol(ep["protocols"], preferred_protocols)
        namespace = ep.get("namespace", DEFAULT_NS)
        result    = {
            "endpoint":     ep["endpoint"],
            "protocol":     protocol,
            "ttl":          5,
            "region":       ep.get("region_label") or ep.get("region", ""),
            "flag":         ep.get("flag", ""),
            "cached":       False,
            "selected_by":  "emergency_fallback",
            "resolution_time_ms": round((_time.monotonic() - t0) * 1000, 1),
            "metadata": {
                "label":            label,
                "total_candidates": len(servers),
                "all_candidates":   all_candidates,
            },
        }
        result = _build_proxy_response(result, label, namespace)
        return result

    best_server, best_health = ranked[0]
    protocol = select_protocol(best_server["protocols"], preferred_protocols)
    ttl      = calculate_ttl(best_health)

    if len(ranked) == 1:
        selected_by = "only_available"
    elif requester_context.get("location"):
        selected_by = "geo_nearest"
    else:
        selected_by = "lowest_latency"

    result = {
        "endpoint":     best_server["endpoint"],
        "protocol":     protocol,
        "ttl":          ttl,
        "region":       best_server["region_label"] or best_server["region"],
        "flag":         best_server.get("flag", ""),
        "cached":       False,
        "selected_by":  selected_by,
        "resolution_time_ms": round((_time.monotonic() - t0) * 1000, 1),
        "metadata": {
            "label":            label,
            "latency_ms":       round(best_health.get("response_time_ms", 0.0), 1),
            "total_candidates": len(servers),
            "all_candidates":   all_candidates,
        },
    }

    # Store with agent_name tag so invalidate() can find it.
    # Use a copy so the pop below doesn't remove the tag from the stored payload.
    result["_cache_key_agent"] = label
    if cache_enabled:
        await _cache.set(cache_key, dict(result), ttl)
    result.pop("_cache_key_agent", None)

    # Enrich with proxy URL + slim_identity when A2A_PROXY_ENDPOINTS is configured
    namespace = best_server.get("namespace", DEFAULT_NS)
    result = _build_proxy_response(result, label, namespace)

    logger.info(
        f"Resolved '{label}': {result.get('url') or result.get('endpoint')} "
        f"({best_health.get('response_time_ms', 0):.0f}ms, ttl={ttl}s, by={selected_by})"
    )
    return result


# ── POST /register ─────────────────────────────────────────────────────────────

@app.post("/register", status_code=200)
@_limit("60/minute")
async def register(request: Request, body: dict):
    """
    Register an agent endpoint.

    Required:
        label     — short name, e.g. "emailer"
        endpoint  — full URL, e.g. "http://host:8080"

    Optional:
        namespace       — URN namespace (default: AGENTNS_NAMESPACE env var)
        region          — e.g. "us-east"
        region_label    — human readable, e.g. "New York, NY"
        location        — {"city": "New York"} or {"latitude": 40.7, "longitude": -74.0}
        protocols       — ["A2A", "http"]   (default: ["http"])
        health_check_url — explicit health URL (probed to verify liveness)
        flag            — emoji flag, e.g. "🇺🇸"
    """
    label    = (body.get("label") or "").strip()
    endpoint = (body.get("endpoint") or "").strip()

    if not label or not endpoint:
        raise HTTPException(status_code=400, detail="'label' and 'endpoint' are required")

    namespace    = body.get("namespace") or DEFAULT_NS
    region       = body.get("region") or ""
    region_label = body.get("region_label") or region
    location     = body.get("location") or {}
    protocols    = body.get("protocols") or ["http"]
    flag         = body.get("flag") or ""

    # Normalise city → lat/lon
    # Resolution order:
    #   1. Explicit lat/lon already in payload → use directly
    #   2. City name in built-in CITY_COORDS table → instant lookup
    #   3. City name unknown → Nominatim geocoding API (free, any city on Earth)
    #   4. Geocoding failed → geo-routing disabled, endpoint still registered
    _location_resolved = False
    if isinstance(location, dict) and location.get("latitude") and location.get("longitude"):
        _location_resolved = True  # Explicit coords — always works, no lookup needed
    elif isinstance(location, dict) and location.get("city"):
        coords = await resolve_city(location["city"])
        if coords:
            location = {**location, "latitude": coords[0], "longitude": coords[1]}
            _location_resolved = True
        # If coords is None, warning already logged by resolve_city()

    # Health check URL — try custom first, then fall back to auto-discovery
    hc_url = (body.get("health_check_url") or "").strip()

    entry: Dict[str, Any] = {
        "endpoint":        endpoint,
        "health_check_url": hc_url,
        "namespace":       namespace,
        "protocols":       protocols,
        "region":          region,
        "region_label":    region_label,
        "flag":            flag,
        "location":        location,
        "agent_name":      build_urn(DEFAULT_TLD, namespace, label),
    }

    _registry.setdefault(label, [])
    existing_urls = [e["endpoint"] for e in _registry[label]]

    if endpoint in existing_urls:
        for e in _registry[label]:
            if e["endpoint"] == endpoint:
                e.update(entry)
        action = "updated"
    else:
        _registry[label].append(entry)
        action = "registered"

    await _save_to_mongo(label, entry)

    # Kick off immediate health check (non-blocking)
    asyncio.create_task(_check_single(endpoint, hc_url))

    logger.info(f"{action}: label={label!r} endpoint={endpoint} region={region_label!r} geo={'active' if _location_resolved else 'disabled'}")
    return {
        "status":          action,
        "label":           label,
        "endpoint":        endpoint,
        "agent_name":      entry["agent_name"],
        "total_endpoints": len(_registry[label]),
        "geo_routing":     "active" if _location_resolved else "disabled — pass latitude/longitude to enable",
    }


# ── DELETE /register/{label} ───────────────────────────────────────────────────

@app.delete("/register/{label}")
async def deregister(
    request: Request,
    label: str,
    endpoint: Optional[str] = None,   # query param: DELETE /register/emailer?endpoint=http://...
    body: Optional[Dict] = None,       # body:        {"endpoint": "http://..."}
):
    """
    Deregister one or all endpoints for *label*.

    Endpoint can be supplied two ways (query param is preferred — more reliable
    through cloud proxies that strip DELETE request bodies):

        DELETE /register/emailer?endpoint=http%3A//host%3A8080   (query param)
        DELETE /register/emailer  {"endpoint": "http://host:8080"} (body)

    Omit endpoint entirely to remove ALL endpoints for the label.
    """
    if label not in _registry:
        raise HTTPException(status_code=404, detail=f"Label '{label}' not found")

    # Query param takes precedence over body; fall back to body for backward compat
    endpoint = (endpoint or (body or {}).get("endpoint") or "").strip()

    if endpoint:
        before = len(_registry[label])
        _registry[label] = [e for e in _registry[label] if e["endpoint"] != endpoint]
        removed = before - len(_registry[label])
        if not _registry[label]:
            del _registry[label]
        if _mongo_col:
            try:
                await _mongo_col.delete_one({"label": label, "endpoint": endpoint})
            except Exception as exc:
                logger.error(f"MongoDB delete failed ({label}/{endpoint}): {exc}")
    else:
        removed = len(_registry.pop(label, []))
        if _mongo_col:
            try:
                await _mongo_col.delete_many({"label": label})
            except Exception as exc:
                logger.error(f"MongoDB delete_many failed ({label}): {exc}")

    return {"status": "deregistered", "label": label, "removed": removed}


# ── GET /health ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    agents_status: Dict[str, List] = {}
    for label, eps in _registry.items():
        agents_status[label] = []
        for ep in eps:
            h = _cached_health(ep["endpoint"])
            agents_status[label].append({
                "endpoint":   ep["endpoint"],
                "region":     ep.get("region_label") or ep.get("region", ""),
                "flag":       ep.get("flag", ""),
                "status":     h.get("status", "unknown"),
                "latency_ms": round(h.get("response_time_ms", 0.0), 1),
                "load":       h.get("load", 50.0),
                "last_check": h.get("last_check"),
            })

    all_statuses = [s["status"] for eps in agents_status.values() for s in eps]
    overall = "ok" if "unhealthy" not in all_statuses else "degraded"

    geocache = geocode_cache_snapshot()
    out: Dict[str, Any] = {
        "ok":                      overall == "ok",
        "status":                  overall,
        "service":                 "agentns",
        "version":                 "3.0.0",
        "namespace":               DEFAULT_NS,
        "tld":                     DEFAULT_TLD,
        "mongodb_connected":       _mongo_col is not None,
        "health_check_interval_s": HEALTH_INTERVAL,
        "total_labels":            len(_registry),
        "total_endpoints":         sum(len(v) for v in _registry.values()),
        "uptime_seconds":          round(_time.time() - _start_time, 1),
        "proxy": {
            "enabled":  bool(_PROXY_ENDPOINTS),
            "mode":     _PROXY_MODE if _PROXY_ENDPOINTS else None,
            "endpoint": _PROXY_ENDPOINTS[0] if _PROXY_ENDPOINTS else None,
            "slim_org": SLIM_ORG or None,
        },
        "switchboard": {
            "enabled":            bool(_federation),
            "remote_registries":  len(_federation),
            "tlds":               list(_federation.keys()),
        },
        "agents": agents_status,
    }
    # Only include geocoded_cities when at least one city has been resolved
    if geocache:
        out["geocoded_cities"] = {
            city: {"lat": c[0], "lon": c[1]} if c else "failed"
            for city, c in geocache.items()
        }
    return out


# ── GET /agents ────────────────────────────────────────────────────────────────

@app.get("/agents")
async def list_agents():
    result: Dict[str, List] = {}
    for label, eps in _registry.items():
        result[label] = []
        for ep in eps:
            h = _cached_health(ep["endpoint"])
            result[label].append({
                "endpoint":   ep["endpoint"],
                "agent_name": ep.get("agent_name", ""),
                "namespace":  ep.get("namespace", ""),
                "region":     ep.get("region_label") or ep.get("region", ""),
                "flag":       ep.get("flag", ""),
                "protocols":  ep.get("protocols", []),
                "status":     h.get("status", "unknown"),
                "latency_ms": round(h.get("response_time_ms", 0.0), 1),
                "last_check": h.get("last_check"),
            })
    return result


# ── GET /namespaces ────────────────────────────────────────────────────────────

@app.get("/namespaces")
async def namespaces():
    ns_map: Dict[str, List[str]] = {}
    for label, eps in _registry.items():
        for ep in eps:
            ns = ep.get("namespace", DEFAULT_NS)
            ns_map.setdefault(ns, [])
            if label not in ns_map[ns]:
                ns_map[ns].append(label)
    return {"tld": DEFAULT_TLD, "namespaces": ns_map}


# ── cache endpoints ────────────────────────────────────────────────────────────

@app.get("/cache/stats")
async def cache_stats():
    return await _cache.stats()


@app.post("/cache/clear")
@_limit("5/minute")
async def cache_clear(request: Request):
    count = await _cache.clear()
    return {"status": "cleared", "entries_removed": count}


# ── Switchboard endpoints ─────────────────────────────────────────────────────

@app.get("/switchboard/registries")
async def switchboard_list():
    """
    List this registry and every connected remote registry.

    The first entry is always this local instance. Remaining entries are
    remote agentns instances registered via POST /switchboard/registries or
    FEDERATION_REGISTRIES env var.
    """
    registries = [
        {
            "registry_id": DEFAULT_TLD,
            "tld":         DEFAULT_TLD,
            "namespace":   DEFAULT_NS,
            "url":         f"http://localhost:{PORT}",
            "type":        "local",
            "status":      "active",
            "labels":      len(_registry),
            "endpoints":   sum(len(v) for v in _registry.values()),
        }
    ]
    for tld, info in _federation.items():
        registries.append({
            "registry_id": info["registry_id"],
            "tld":         tld,
            "url":         info["url"],
            "type":        "remote",
            "status":      info.get("status", "configured"),
            "added_at":    info.get("added_at"),
        })
    return {"registries": registries, "count": len(registries)}


@app.post("/switchboard/registries")
async def switchboard_register(body: dict):
    """
    Connect a remote agentns instance to the switchboard.

    Required fields:
        tld  — the TLD the remote registry owns  e.g. "payments.acme.io"
        url  — base URL of the remote instance   e.g. "http://payments-agentns:8200"

    Optional:
        registry_id — human-readable label (defaults to tld)

    After registering, /resolve requests for URNs whose TLD matches will be
    automatically forwarded to the remote registry.
    """
    tld = (body.get("tld") or "").strip()
    url = (body.get("url") or "").strip().rstrip("/")
    if not tld or not url:
        raise HTTPException(400, "'tld' and 'url' are required")
    if tld == DEFAULT_TLD:
        raise HTTPException(400, f"'{tld}' is this instance's own TLD — cannot register as remote")

    registry_id = (body.get("registry_id") or tld).strip()

    # Probe the remote to confirm it's reachable and report its status
    remote_status = "unreachable"
    try:
        resp = await _proxy_client.get(
            f"{url}/health",
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0),
        )
        remote_status = "active" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception:
        pass  # unreachable — still register; it may come up later

    _federation[tld] = {
        "url":         url,
        "registry_id": registry_id,
        "status":      remote_status,
        "added_at":    datetime.now(timezone.utc).isoformat(),
    }
    logger.info(f"Switchboard: registered tld={tld!r} url={url!r} status={remote_status}")
    return {
        "status":        "registered",
        "tld":           tld,
        "url":           url,
        "registry_id":   registry_id,
        "remote_status": remote_status,
    }


@app.delete("/switchboard/registries/{tld:path}")
async def switchboard_deregister(tld: str):
    """
    Disconnect a remote registry from the switchboard.

    After removal, /resolve for that TLD returns 404 instead of routing.
    """
    if tld not in _federation:
        raise HTTPException(404, f"No remote registry registered for TLD '{tld}'")
    _federation.pop(tld)
    logger.info(f"Switchboard: removed remote registry tld={tld!r}")
    return {"status": "removed", "tld": tld}


# ── proxy helpers ─────────────────────────────────────────────────────────────

# Headers that must not be forwarded (hop-by-hop per RFC 2616 §13.5.1)
_HOP_BY_HOP = frozenset([
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length",
])


def _proxy_target(label: str) -> str:
    """Return the best healthy endpoint URL for *label*, or raise 404/503."""
    endpoints = _registry.get(label)
    if not endpoints:
        raise HTTPException(status_code=404, detail=f"No endpoints registered for label '{label}'")

    servers = [
        {
            "server_id":        ep["endpoint"],
            "endpoint":         ep["endpoint"],
            "health_check_url": ep.get("health_check_url", ""),
            "protocols":        ep.get("protocols", []),
            "region":           ep.get("region", ""),
            "region_label":     ep.get("region_label", ep.get("region", "")),
            "location":         ep.get("location", {}),
        }
        for ep in endpoints
    ]
    health_map = {s["server_id"]: _cached_health(s["server_id"]) for s in servers}
    ranked     = rank_servers(servers, health_map, {})
    return ranked[0][0]["endpoint"] if ranked else servers[0]["endpoint"]


# ── ANY /proxy/{label}[/{path}] ────────────────────────────────────────────────

@app.api_route(
    "/proxy/{label}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@app.api_route(
    "/proxy/{label}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_agent(request: Request, label: str, path: str = ""):
    """
    Forward a request to the best healthy endpoint registered under *label*.

    URL patterns
    ------------
        /proxy/{label}                      → {endpoint}/
        /proxy/{label}/chat                 → {endpoint}/chat
        /proxy/{label}/.well-known/agent.json → fetched and A2A url field rewritten

    A2A support
    -----------
    - For /.well-known/agent.json the response `url` field is rewritten to
      point back at this proxy so callers never bypass it on future requests.
    - The A2A method (message/send, message/stream, etc.) is extracted from
      the request body and added to structured log lines as a2a=<method>.

    Streaming
    ---------
    Server-Sent Events (text/event-stream) are forwarded transparently using
    StreamingResponse so message/stream works end-to-end.
    """
    t0 = _time.monotonic()

    # ── 1. pick best healthy endpoint ─────────────────────────────────────────
    target_ep  = _proxy_target(label)
    target_url = target_ep.rstrip("/") + ("/" + path if path else "")
    if request.query_params:
        target_url += "?" + str(request.query_params)

    # ── 2. A2A agent card — rewrite url to point at this proxy ───────────────
    if path == ".well-known/agent.json":
        try:
            card_resp = await _proxy_client.get(target_url)
            data = card_resp.json()
            proxy_root  = str(request.base_url).rstrip("/")
            data["url"] = f"{proxy_root}/proxy/{label}"
            logger.info(f"proxy: agent_card label={label!r} url rewritten → {data['url']!r}")
            return data
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not fetch agent card: {exc}")

    # ── 3. read body; extract A2A method for logging ──────────────────────────
    body       = await request.body()
    a2a_method = ""
    if body:
        try:
            a2a_method = json.loads(body).get("method", "")
        except Exception:
            pass

    # ── 4. strip hop-by-hop headers ───────────────────────────────────────────
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    # ── 5. forward and stream the response back ────────────────────────────────
    # Use the shared _proxy_client — no new client per request (connection reuse).
    try:
        upstream_req = _proxy_client.build_request(
            method  = request.method,
            url     = target_url,
            headers = fwd_headers,
            content = body,
        )
        upstream = await _proxy_client.send(upstream_req, stream=True)

        status       = upstream.status_code
        content_type = upstream.headers.get("content-type", "application/octet-stream")
        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }

        elapsed = round((_time.monotonic() - t0) * 1000, 1)
        logger.info(
            f"proxy: {request.method} /proxy/{label}/{path} → {target_ep} "
            f"[{status}] {elapsed}ms"
            + (f" a2a={a2a_method!r}" if a2a_method else "")
        )

        # SSE / chunked — stream bytes directly to caller
        if "text/event-stream" in content_type:
            async def _stream_sse():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await upstream.aclose()   # close response only, NOT the shared client
            return StreamingResponse(
                _stream_sse(),
                status_code = status,
                headers     = resp_headers,
                media_type  = content_type,
            )

        # Normal response — buffer then return
        try:
            content = await upstream.aread()
        finally:
            await upstream.aclose()   # close response only, NOT the shared client

        return Response(
            content     = content,
            status_code = status,
            headers     = resp_headers,
            media_type  = content_type,
        )

    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"Could not connect to '{label}' at {target_ep}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Agent '{label}' at {target_ep} timed out")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Proxy error: {exc}")


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point: ``agentns-server`` or ``python -m agentns``."""
    import argparse

    parser = argparse.ArgumentParser(description="agentns — Agent Name Service sidecar")
    parser.add_argument("--port",     type=int, default=PORT,            help="HTTP port (default: 8200)")
    parser.add_argument("--host",     type=str, default="0.0.0.0",       help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--log-level",type=str, default="info",          help="Log level (default: info)")
    parser.add_argument("--namespace",type=str, default=DEFAULT_NS,      help="Default URN namespace")
    args = parser.parse_args()

    if args.namespace != DEFAULT_NS:
        os.environ["AGENTNS_NAMESPACE"] = args.namespace

    _proxy_display = _PROXY_ENDPOINTS[0] if _PROXY_ENDPOINTS else "disabled"
    _fed_display   = f"{len(_federation)} registr(ies): {list(_federation)}" if _federation else "disabled"
    print(f"""
╔══════════════════════════════════════════════╗
║          agentns  v3.0.0  starting           ║
╚══════════════════════════════════════════════╝
  Port         : {args.port}
  TLD          : {DEFAULT_TLD}
  Namespace    : {args.namespace}
  MongoDB      : {'enabled' if MONGODB_URI else 'disabled (in-memory)'}
  Health       : every {HEALTH_INTERVAL}s
  Proxy        : {_proxy_display}
  Switchboard  : {_fed_display}
""")
    uvicorn.run(
        "agentns.server:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
