"""
agentns.server
==============
Single-binary Agent Name Service sidecar.

Combines all three resolution hops into one process:

    Recursive Resolver  →  Namespace Registry  →  Authoritative NS

Any agent framework in any language calls the HTTP API:

    POST /resolve     { "agent_name": "urn:myco.com:sales:emailer" }
    POST /register    { "label": "emailer", "endpoint": "http://..." }
    GET  /health
    GET  /agents
    POST /cache/clear
    GET  /cache/stats

Configuration (environment variables — zero hardcoded values)
-------------------------------------------------------------
    AGENTNS_PORT              HTTP port            (default: 8200)
    AGENTNS_NAMESPACE         Default URN namespace (default: "agents.local")
    AGENTNS_TLD               URN TLD              (default: "agentns.local")
    AGENTNS_HEALTH_INTERVAL   Background health sweep interval in s  (default: 30)
    MONGODB_URI               MongoDB connection string (optional; in-memory if absent)
    MONGODB_DB                MongoDB database name    (default: "agentns")
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException

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

# ── config from env ────────────────────────────────────────────────────────────
PORT             = int(os.getenv("AGENTNS_PORT",            "8200"))
DEFAULT_NS       = os.getenv("AGENTNS_NAMESPACE",           "agents.local")
DEFAULT_TLD      = os.getenv("AGENTNS_TLD",                 "agentns.local")
HEALTH_INTERVAL  = int(os.getenv("AGENTNS_HEALTH_INTERVAL", "30"))
MONGODB_URI      = os.getenv("MONGODB_URI",                 "")
MONGODB_DB       = os.getenv("MONGODB_DB",                  "agentns")

_start_time = _time.time()

# ── in-memory registry ─────────────────────────────────────────────────────────
# { label -> [endpoint_dict, ...] }
_registry: Dict[str, List[Dict]] = {}

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
        doc = {k: v for k, v in entry.items()}
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
    await _init_mongo()
    await _load_from_mongo()
    await _check_all()          # initial sweep so first /resolve has real data
    task = asyncio.create_task(_health_loop())

    total = sum(len(v) for v in _registry.values())
    logger.info(f"agentns ready — {total} endpoint(s) across {len(_registry)} label(s) | port {PORT}")

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="agentns — Agent Name Service",
    description=(
        "Single-binary service discovery sidecar for multi-agent systems.\n\n"
        "Register agents with POST /register. Resolve them with POST /resolve using "
        "standard URNs (urn:tld:namespace:label). Language-agnostic HTTP API."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── POST /resolve ──────────────────────────────────────────────────────────────

@app.post("/resolve")
async def resolve(body: dict):
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

        # ── Namespace validation ───────────────────────────────────────────────
        # If the URN contains a TLD or namespace, they must match this instance.
        # This makes agentns behave like DNS — it only answers for its own domain.
        #
        # Examples (DEFAULT_TLD="agents.dataworksai.com", DEFAULT_NS="mbta-transit-ci"):
        #
        #   urn:agents.dataworksai.com:mbta-transit-ci:alerts  → OK
        #   urn:agents.dataworksai.com:alerts                  → OK (namespace omitted)
        #   urn:wrong.com:mbta-transit-ci:alerts               → 403 wrong TLD
        #   urn:agents.dataworksai.com:other-app:alerts        → 403 wrong namespace
        #   alerts                                             → OK (plain label, no check)

        if parsed.tld and parsed.tld != DEFAULT_TLD:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This agentns instance handles TLD '{DEFAULT_TLD}', "
                    f"not '{parsed.tld}'. You are asking the wrong nameserver."
                ),
            )

        if parsed.namespace and parsed.namespace != DEFAULT_NS:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This agentns instance handles namespace '{DEFAULT_NS}', "
                    f"not '{parsed.namespace}'. "
                    f"Register agents under the correct namespace or update AGENTNS_NAMESPACE."
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
        ep       = servers[0]
        protocol = select_protocol(ep["protocols"], preferred_protocols)
        result   = {
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

    # Store with agent_name tag so invalidate() can find it
    result["_cache_key_agent"] = label
    if cache_enabled:
        await _cache.set(cache_key, result, ttl)
    result.pop("_cache_key_agent", None)

    logger.info(
        f"Resolved '{label}': {best_server['endpoint']} "
        f"({best_health.get('response_time_ms', 0):.0f}ms, ttl={ttl}s, by={selected_by})"
    )
    return result


# ── POST /register ─────────────────────────────────────────────────────────────

@app.post("/register", status_code=200)
async def register(body: dict):
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
async def deregister(label: str, body: Optional[Dict] = None):
    """
    Deregister one or all endpoints for *label*.

    Body (optional):
        { "endpoint": "http://host:8080" }   — remove specific endpoint
        {}                                   — remove all endpoints for label
    """
    if label not in _registry:
        raise HTTPException(status_code=404, detail=f"Label '{label}' not found")

    body     = body or {}
    endpoint = (body.get("endpoint") or "").strip()

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
    return {
        "ok":                     overall == "ok",
        "status":                 overall,
        "service":                "agentns",
        "version":                "1.0.0",
        "namespace":              DEFAULT_NS,
        "tld":                    DEFAULT_TLD,
        "mongodb_connected":      _mongo_col is not None,
        "health_check_interval_s": HEALTH_INTERVAL,
        "total_labels":           len(_registry),
        "total_endpoints":        sum(len(v) for v in _registry.values()),
        "uptime_seconds":         round(_time.time() - _start_time, 1),
        "geocoded_cities":        {
            city: {"lat": c[0], "lon": c[1]} if c else "failed"
            for city, c in geocache.items()
        },
        "agents":                 agents_status,
    }


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
async def cache_clear():
    count = await _cache.clear()
    return {"status": "cleared", "entries_removed": count}


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

    print(f"""
╔══════════════════════════════════════════════╗
║          agentns  v1.0.0  starting           ║
╚══════════════════════════════════════════════╝
  Port      : {args.port}
  Namespace : {args.namespace}
  TLD       : {DEFAULT_TLD}
  MongoDB   : {'connected' if MONGODB_URI else 'disabled (in-memory)'}
  Health    : every {HEALTH_INTERVAL}s
""")
    uvicorn.run(
        "agentns.server:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
