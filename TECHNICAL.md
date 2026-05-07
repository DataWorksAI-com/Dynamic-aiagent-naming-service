# agentns — Technical Reference

**Version:** 3.0.0  
**License:** MIT

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Startup Sequence](#3-startup-sequence)
4. [End-to-End Flows](#4-end-to-end-flows)
   - 4.1 [Agent Registration Flow](#41-agent-registration-flow)
   - 4.2 [Proxy Call Flow](#42-proxy-call-flow)
   - 4.3 [Resolution Flow (Cache Hit)](#43-resolution-flow-cache-hit)
   - 4.4 [Resolution Flow (Cache Miss)](#44-resolution-flow-cache-miss)
   - 4.5 [Background Health Sweep](#45-background-health-sweep)
   - 4.6 [Deregistration Flow](#46-deregistration-flow)
5. [Module Reference](#5-module-reference)
   - 5.1 [server.py — Main Application](#51-serverpy--main-application)
   - 5.2 [Built-in Proxy](#52-built-in-proxy)
   - 5.3 [health_checker.py — Health Probing](#53-health_checkerpy--health-probing)
   - 5.4 [server_selection.py — Ranking Engine](#54-server_selectionpy--ranking-engine)
   - 5.5 [geo_policy.py — Geo Routing Policies](#55-geo_policypy--geo-routing-policies)
   - 5.6 [geocoder.py — City Resolution](#56-geocoderpy--city-resolution)
   - 5.7 [cache.py — Resolution Cache](#57-cachepy--resolution-cache)
   - 5.8 [urn_parser.py — URN Parsing](#58-urn_parserpy--urn-parsing)
   - 5.9 [registry_adapter.py — Pluggable Backends](#59-registry_adapterpy--pluggable-backends)
   - 5.10 [auth.py — Security Headers](#510-authpy--security-headers)
   - 5.11 [target_lib.py — Agent Registration SDK](#511-target_libpy--agent-registration-sdk)
   - 5.12 [requester_lib.py — Agent Resolution SDK](#512-requester_libpy--agent-resolution-sdk)
6. [Data Models](#6-data-models)
7. [Server Selection Algorithm](#7-server-selection-algorithm)
8. [MongoDB Integration](#8-mongodb-integration)
9. [Concurrency Model](#9-concurrency-model)
10. [API Reference](#10-api-reference)
11. [Configuration Reference](#11-configuration-reference)
12. [Error Handling](#12-error-handling)
13. [Performance Characteristics](#13-performance-characteristics)
14. [Deployment Guide](#14-deployment-guide)
15. [Development Guide](#15-development-guide)
16. [Switchboard / Federation](#16-switchboard--federation)

---

## 1. System Overview

agentns is a **service discovery and proxy sidecar** for multi-agent AI systems. It solves the same problem that DNS solves for the internet — but for AI agents: you call an agent by name, agentns finds the best healthy replica, and either forwards your request directly or tells you where to send it.

### The Core Problem

In a multi-agent system, orchestrators need to find and call other agents by name. Without a discovery layer, agent URLs are hardcoded. This breaks when:
- Agents scale horizontally (multiple replicas in different regions)
- Agents move between hosts or clouds
- An agent goes down and a replica must take over
- Geographic routing is needed (nearest healthy replica)
- You want the proxy/routing logic decoupled from each caller

### What agentns Does

agentns runs as a **sidecar process** alongside your orchestrator. Agents register themselves on startup. Callers either:

1. **Call through the proxy** — `POST /proxy/alerts` — agentns resolves + forwards to the best instance
2. **Resolve first, then call** — `POST /resolve` — get the URL back, call directly

Both approaches use the same health-aware, geo-ranked selection engine.

### Design Principles

| Principle | Implementation |
|-----------|---------------|
| **Zero hardcoded values** | Every URL and name comes from environment variables |
| **Language-agnostic** | Plain HTTP API — Python, Go, Node.js, Java, curl all work |
| **Graceful degradation** | Never crashes the caller — returns emergency fallback if all replicas unhealthy |
| **No auth overhead** | No API keys required — designed for sidecar/internal network deployments |
| **Self-healing** | Background health loop continuously re-evaluates endpoints, auto-recovers when agents come back |
| **Optional persistence** | In-memory mode works with zero setup; MongoDB mode survives restarts |

---

## 2. Architecture

### Component Map

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              agentns Process                              │
│                                                                          │
│  ┌──────────────────┐    ┌───────────────────────────────────────────┐  │
│  │   FastAPI app     │    │              Global State                  │  │
│  │   (server.py)    │    │                                            │  │
│  │                  │    │  _registry:     Dict[label, [ep,...]]      │  │
│  │  ANY /proxy/{l}  │    │  _health_cache: Dict[url, health_dict]    │  │
│  │  POST /register  │    │  _cache:        ResolutionCache            │  │
│  │  DELETE /register│    │  _mongo_col:    Collection | None          │  │
│  │  POST /resolve   │    │  _PROXY_ENDPOINTS: List[str]              │  │
│  │  GET /health     │    └───────────────────────────────────────────┘  │
│  │  GET /agents     │                                                    │
│  │  GET /namespaces │                                                    │
│  └──────┬───────────┘                                                    │
│         │ calls                                                          │
│  ┌──────▼────────────────────────────────────────────────────────────┐  │
│  │                         Core Modules                               │  │
│  │                                                                    │  │
│  │  urn_parser.py       → parse / build / validate URNs              │  │
│  │  health_checker.py   → async HTTP health probes                   │  │
│  │  server_selection.py → rank endpoints by 5-key sort               │  │
│  │  geo_policy.py       → pluggable geo strategies                   │  │
│  │  geocoder.py         → city name → lat/lon (Nominatim fallback)   │  │
│  │  cache.py            → TTL resolution cache                       │  │
│  │  registry_adapter.py → pluggable registry backends                │  │
│  │  auth.py             → security headers middleware                 │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                  Background asyncio Task                           │  │
│  │   _health_loop() — runs every AGENTNS_HEALTH_INTERVAL seconds     │  │
│  │   → _check_all() → parallel health probes (asyncio.gather)       │  │
│  │   → _cache.purge_expired()                                        │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                  MongoDB (optional)                                │  │
│  │   Collection: agentns.agents                                      │  │
│  │   Indexes: label (single), (label + endpoint) unique compound     │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
         ▲                    ▲                         ▲
         │ POST /register     │ ANY /proxy/{label}      │ POST /resolve
         │                    │                         │
   ┌─────┴──────┐    ┌────────┴────────┐      ┌────────┴──────┐
   │   Agent    │    │   Any Caller    │      │  Orchestrator  │
   │ (any lang) │    │ (resolve+proxy) │      │ (resolve only) │
   └────────────┘    └─────────────────┘      └───────────────┘
```

### Module Dependency Graph

```
server.py
   ├── urn_parser.py         (parse_urn, build_urn, extract_label)
   ├── health_checker.py     (check_agent_health, probe_endpoint)
   ├── server_selection.py   (rank_servers, select_protocol, calculate_ttl)
   │     └── geo_policy.py   (NearestPolicy, LeastLoadedPolicy, CompositePolicy)
   │     └── geocoder.py     (city_to_latlon, Nominatim fallback)
   ├── cache.py              (ResolutionCache)
   ├── registry_adapter.py   (HttpRegistryAdapter, StaticRegistryAdapter, ...)
   └── auth.py               (security_headers_middleware)

target_lib.py                (standalone SDK — calls server.py via HTTP)
requester_lib.py             (standalone SDK — calls server.py via HTTP)
```

### Single-Binary Design

Traditional ANS architectures use separate networked hops:

```
Traditional:  Caller → Recursive Resolver → Registry NS → Auth NS
agentns:      Caller → agentns (all hops in one process)
```

agentns collapses all three into one in-process call chain, eliminating network overhead while keeping the same logical resolution model.

---

## 3. Startup Sequence

When agentns starts (`agentns-server` or `docker run`), the following executes before any HTTP request is accepted:

```
Process starts
     │
     ▼
main() in server.py
  └─ parse CLI args (--port, --host, --namespace, --log-level)
  └─ uvicorn.run("agentns.server:app", ...)
         │
         ▼
     FastAPI lifespan() begins (asynccontextmanager)
         │
         ├─ Step 1: _init_mongo()
         │     If MONGODB_URI is set:
         │       - Create AsyncIOMotorClient (6s selection timeout)
         │       - Get database[MONGODB_DB] → collection "agents"
         │       - Create index on "label"
         │       - Create unique compound index on (label, endpoint)
         │       - Ping to verify connectivity
         │       - Set _mongo_col = collection handle
         │     If MONGODB_URI is empty or connection fails:
         │       - Log warning, leave _mongo_col = None
         │       - Continue in in-memory mode (never blocks startup)
         │
         ├─ Step 2: _load_from_mongo()
         │     If _mongo_col is not None:
         │       - Stream all documents from the collection
         │       - Restore _registry[label] list for every persisted endpoint
         │       - Skip duplicates (idempotent)
         │       - Agents registered in previous process runs reappear instantly
         │
         ├─ Step 3: _check_all()  ← initial health sweep
         │     - Deduplicate {endpoint_url → health_check_url} from _registry
         │     - asyncio.gather() all health probes in parallel
         │     - Write results to _health_cache
         │     - First /resolve call has real health data immediately
         │
         ├─ Step 4: asyncio.create_task(_health_loop())
         │     - Non-blocking background task started
         │     - Runs every AGENTNS_HEALTH_INTERVAL seconds
         │
         ├─ Print startup banner:
         │     "agentns ready — N endpoint(s) | port P | proxy: <url or disabled>"
         │
         └─ yield  ← server accepts HTTP requests

Shutdown (SIGTERM / Ctrl+C):
     lifespan resumes after yield
       └─ task.cancel()        ← signal background loop to stop
       └─ await task           ← wait for clean exit
       └─ except CancelledError ← expected, suppressed
```

---

## 4. End-to-End Flows

### 4.1 Agent Registration Flow

An agent calls `POST /register` on startup. Step-by-step:

```
Agent process                agentns server
─────────────                ─────────────
POST /register
{
  "label": "emailer",
  "endpoint": "http://ny-host:9001",
  "region": "us-east",
  "location": {"city": "New York"},
  "protocols": ["A2A"],
  "health_check_url": "http://ny-host:9001/health"
}
                         ─────────────────────────►

                              1. Validate input
                                 label + endpoint required → HTTP 400 if missing

                              2. Normalize location
                                 city "New York" → CITY_COORDS → lat/lon injected
                                 Enables geo-routing without caller knowing coords

                              3. Build URN
                                 build_urn(DEFAULT_TLD, namespace, label)
                                 → "urn:agentns.local:agents.local:emailer"

                              4. Build entry dict
                                 {endpoint, health_check_url, namespace,
                                  protocols, region, region_label, flag,
                                  location, agent_name}

                              5. Check _registry[label]
                                 Same endpoint → update in place
                                 New endpoint   → append to list
                                 (multiple endpoints = replica pool)

                              6. _save_to_mongo(label, entry)
                                 Upsert:  filter={label, endpoint}
                                          $set={all fields, last_seen=now}
                                          $setOnInsert={registered_at=now}
                                 No-op if MongoDB not configured

                              7. asyncio.create_task(_check_single(...))
                                 Background health probe — does not delay response

                         ◄─────────────────────────
                              {
                                "status": "registered",
                                "label": "emailer",
                                "endpoint": "http://ny-host:9001",
                                "agent_name": "urn:agentns.local:agents.local:emailer",
                                "total_endpoints": 1,
                                "geo_routing": "active"
                              }
```

**Key behavior:** Same `(label, endpoint)` → updated (no duplicate). New endpoint under same label → appended (replica pool). This is how multi-region failover works.

---

### 4.2 Proxy Call Flow

The caller sends a request to agentns. agentns resolves the label, forwards the request to the best endpoint, and streams the response back. The caller never needs to know the real endpoint URL.

```
Caller                       agentns server               Target Agent
──────                       ─────────────                ────────────

POST /proxy/alerts
{"method":"message/send",
 "params":{"text":"hi"}}
                         ─────────────────────────►

                              1. Extract label from URL path
                                 label = "alerts"

                              2. _proxy_target("alerts")
                                 → same logic as /resolve (cache-aware)
                                 → HTTP 404 if label not registered

                              3. Construct target URL
                                 target_url = f"{best_endpoint}/{path}"
                                 e.g. "http://ny-host:9001"

                              4. Special case: /.well-known/agent.json
                                 Forward to target, parse JSON response
                                 Rewrite "url" field to point at proxy:
                                   card["url"] = "http://agentns:8200/proxy/alerts"
                                 Returns rewritten card — callers always see
                                 the proxy address, never the real endpoint

                              5. For all other paths:
                                 Read request body
                                 Extract A2A "method" field for logging (if JSON)
                                 Strip hop-by-hop headers:
                                   (connection, keep-alive, transfer-encoding,
                                    te, trailer, upgrade, proxy-authorization,
                                    proxy-authenticate, host)

                              6. httpx.AsyncClient stream request
                                 Method: same as incoming request
                                 URL: target_url
                                 Headers: cleaned incoming headers + correct Host
                                 Body: forwarded as-is
                                                          ─────────────────────►
                                                          GET/POST/PUT/... to real
                                                          agent endpoint
                                                          ◄─────────────────────

                              7. SSE detection
                                 If response Content-Type: text/event-stream
                                   → StreamingResponse (generator yields chunks)
                                   → Caller receives SSE in real time
                                 Else
                                   → Read full body, return Response

                         ◄─────────────────────────
                              Response from target agent
                              (status code, headers, body all preserved)
```

**Agent card rewriting (step 4):**
When a caller fetches `GET /proxy/alerts/.well-known/agent.json`, the agent card's `url` field is rewritten from `http://ny-host:9001` to `http://agentns:8200/proxy/alerts`. This means subsequent calls from tools that discover the agent card will automatically route through the proxy — enabling transparent health-aware failover for A2A protocol users.

---

### 4.3 Resolution Flow (Cache Hit)

The fast path — repeat resolution within TTL window:

```
Orchestrator                 agentns server
────────────                 ─────────────
POST /resolve
{"label": "emailer",
 "requester_context": {
   "location": {"city": "Boston"},
   "protocols": ["A2A"]
 }}
                         ─────────────────────────►

                              1. Parse identifier → label = "emailer"

                              2. Build cache key
                                 MD5("emailer" | ["A2A"] | {"city":"Boston"})
                                 → "a3f2c1..." (deterministic hex digest)

                              3. _cache.get(key)
                                 → entry found AND monotonic() < expiry
                                 → hits counter incremented

                              4. Inject resolution_time_ms, set cached=True

                         ◄─────────────────────────
                              {
                                "endpoint": "http://ny-host:9001",
                                "url":      "http://ny-host:9001",
                                "protocol": "A2A",
                                "ttl": 60,
                                "cached": true,
                                "resolution_time_ms": 0.3
                              }
```

Cache hit round-trip: **< 1 ms** (in-process dict lookup + MD5).

---

### 4.4 Resolution Flow (Cache Miss)

The full resolution path — first call or after TTL expiry:

```
Orchestrator                 agentns server
────────────                 ─────────────
POST /resolve
{"label": "emailer",
 "requester_context": {
   "location": {"city": "Paris"},
   "protocols": ["A2A"]
 }}
                         ─────────────────────────►

                              1. Parse → label = "emailer", cache miss

                              2. Registry lookup
                                 endpoints = _registry["emailer"]
                                 → [nyc_entry, london_entry]
                                 HTTP 404 if label not registered

                              3. Build servers list + health map
                                 Read _health_cache for each endpoint
                                 Live-check any "unknown" endpoints inline
                                   (parallel asyncio.gather)

                              4. rank_servers(servers, health_map, ctx)
                                 Paris lat/lon injected from CITY_COORDS

                                 NYC:    (healthy, A2A✓, 5837km, 45ms, 30%)
                                 London: (healthy, A2A✓,  341km, 210ms, 20%)

                                 London wins — geo_km 341 < 5837
                                 selected_by = "geo_nearest"

                              5. select_protocol → "A2A"
                                 calculate_ttl(healthy) → 60s

                              6. Build result, _cache.set(key, result, 60)

                         ◄─────────────────────────
                              {
                                "endpoint": "http://lon-host:9001",
                                "url":      "http://lon-host:9001",
                                "protocol": "A2A",
                                "ttl": 60,
                                "region": "London, UK",
                                "cached": false,
                                "selected_by": "geo_nearest",
                                "resolution_time_ms": 3.7,
                                "metadata": {
                                  "label": "emailer",
                                  "total_candidates": 2,
                                  "all_candidates": [...]
                                }
                              }
```

---

### 4.5 Background Health Sweep

This loop runs concurrently with all HTTP requests. It is the core of automatic failover and recovery:

```
asyncio event loop
      ├── (HTTP requests served here)
      └── _health_loop() [background task]
              ├── loop forever:
              │
              │   _check_all()
              │     ├── Build deduped {endpoint_url → health_check_url} from _registry
              │     ├── asyncio.gather(*[_check_one(url, hc) for each], return_exceptions=True)
              │     │     └── _check_one():
              │     │           check_agent_health(hc_url) or probe_endpoint(url)
              │     │           async with _health_lock: _health_cache[url] = result
              │     └── All probes run in parallel; one failure doesn't cancel others
              │
              │   _cache.purge_expired()
              │     └── Remove entries where monotonic() > expiry
              │
              └── asyncio.sleep(AGENTNS_HEALTH_INTERVAL)  ← default 30s
```

**Failover:** endpoint goes down → next sweep marks it unhealthy → next resolve skips it automatically.  
**Recovery:** endpoint comes back → next sweep marks it healthy → immediately available for routing.

---

### 4.6 Deregistration Flow

```
Agent shutting down          agentns server
───────────────              ─────────────
DELETE /register/emailer
{"endpoint": "http://ny-host:9001"}
                         ─────────────────────────►

                              1. HTTP 404 if label not in _registry

                              2. endpoint provided → remove specific entry
                                 list becomes empty → delete key entirely

                              No endpoint → remove all for label

                              3. MongoDB: delete_one({label, endpoint})
                                          or delete_many({label})

                         ◄─────────────────────────
                              {"status": "deregistered", "label": "emailer", "removed": 1}
```

---

## 5. Module Reference

### 5.1 `server.py` — Main Application

**File:** `agentns/server.py`  
**Purpose:** FastAPI application. Owns all HTTP endpoints, global state, startup/shutdown, MongoDB integration, background health loop, and the built-in proxy.

#### Global State

```python
_registry: Dict[str, List[Dict]]
```
Primary data structure. Maps every registered `label` to a list of endpoint entry dicts. One label = one replica pool.

```python
_health_cache: Dict[str, Dict]
```
Maps endpoint URL → most recent health result. Written by `_check_all()`, `_check_single()`, and live-check block inside `resolve()`. Protected by `_health_lock`.

```python
_health_lock: asyncio.Lock
```
Prevents concurrent writes to `_health_cache` from the background loop and inline resolve checks.

```python
_cache: ResolutionCache
```
TTL resolution cache. Holds fully-resolved response payloads keyed by MD5 of (label + protocols + location).

```python
_mongo_col: Optional[AsyncIOMotorCollection]
```
Handle to the MongoDB `agents` collection. `None` if not configured or connection failed.

#### Proxy Configuration

```python
_PROXY_HOST      = os.getenv("AGENTNS_PROXY_HOST", "").strip()
_PROXY_PORT      = os.getenv("AGENTNS_PROXY_PORT", "8400").strip()
_PROXY_MODE      = os.getenv("AGENTNS_PROXY_MODE", "agentgateway").lower().strip()
SLIM_ORG         = os.getenv("SLIM_ORG", "")
_raw_proxy_eps   = os.getenv("A2A_PROXY_ENDPOINTS", "")
```

Resolution order for `_PROXY_ENDPOINTS`:
1. `A2A_PROXY_ENDPOINTS` — explicit comma-separated list (lowest level, overrides everything)
2. `AGENTNS_PROXY_HOST` + `AGENTNS_PROXY_PORT` — high-level host/port pair
3. Empty list — proxy passthrough disabled

#### Core Functions (summary)

| Function | Called By | Purpose |
|----------|-----------|---------|
| `_init_mongo()` | lifespan | Connect to MongoDB, create indexes |
| `_load_from_mongo()` | lifespan | Restore registry from MongoDB |
| `_save_to_mongo(label, entry)` | register() | Upsert endpoint to MongoDB |
| `_check_all()` | lifespan + _health_loop | Parallel health probe of all endpoints |
| `_check_single(url, hc)` | register() | One-shot probe for freshly registered endpoint |
| `_health_loop()` | lifespan (task) | Infinite background sweep loop |
| `_cached_health(url)` | resolve(), health() | Safe read from _health_cache with default |
| `_proxy_target(label)` | proxy_agent() | Resolve label to best endpoint URL |
| `lifespan()` | FastAPI | Startup + shutdown orchestration |

---

### 5.2 Built-in Proxy

**Routes:**
```python
@app.api_route("/proxy/{label}", methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
@app.api_route("/proxy/{label}/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
async def proxy_agent(request: Request, label: str, path: str = ""):
```

**Purpose:** Forward any HTTP request to the best healthy instance of the named agent. The caller never needs to know the real endpoint URL.

#### `_HOP_BY_HOP` frozenset

Headers that must be stripped before forwarding (RFC 7230):
```python
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade",
    "proxy-authorization", "proxy-authenticate", "host",
})
```

These headers are specific to the current connection and must not be forwarded to the upstream. Forwarding `host` in particular would break virtual-host routing on the target.

#### `_proxy_target(label) → str` (async)

Resolves a label to the best endpoint URL using the same logic as `POST /resolve` (reads `_registry`, applies `rank_servers()`, uses `_cache`). Returns the raw endpoint URL string. Raises `HTTPException(404)` if the label is unknown.

Called internally by `proxy_agent()` only.

#### `proxy_agent()` — full logic

1. `_proxy_target(label)` → `base_url`. HTTP 404 if label not registered.
2. Construct `target_url = f"{base_url}/{path}"` (path may be empty).
3. **Agent card rewriting** — if `path == ".well-known/agent.json"`:
   - Forward request to target
   - Parse JSON response
   - Overwrite `card["url"]` with `f"{request.base_url}proxy/{label}"` (proxy address)
   - Return rewritten JSON — A2A callers see proxy URL in agent card, enabling transparent routing
4. **All other paths:**
   - Read full request body
   - Extract A2A `method` field from JSON body for access logging (silently ignored if body is not JSON)
   - Build forwarding headers: start with incoming headers, remove hop-by-hop, set correct `host`
   - Open `httpx.AsyncClient()` stream request (same HTTP method, target URL, cleaned headers, body)
   - If response `Content-Type: text/event-stream` → yield chunks as `StreamingResponse` (real-time SSE)
   - Else → await full response body, return `Response` with status code and response headers

**Error handling:**
- `httpx.ConnectError` → HTTP 502 Bad Gateway
- `httpx.TimeoutException` → HTTP 504 Gateway Timeout
- Any other httpx error → HTTP 502

---

### 5.3 `health_checker.py` — Health Probing

**File:** `agentns/health_checker.py`  
**Purpose:** Async HTTP health checking. Probes agent endpoints and returns a normalized health dict.

#### Constants

```python
CONNECT_TIMEOUT = 5.0   # seconds to establish TCP connection
READ_TIMEOUT    = 5.0   # seconds to read response body
SLOW_MS         = 2000  # ms above which status → "degraded"
```

#### `_get_client() → httpx.AsyncClient`

Lazy singleton httpx client. Configured with:
- Connection pool: up to 100 connections, 20 keepalive
- Auto-redirect following
- Separate connect, read, write, pool timeouts

Reusing a singleton avoids TLS handshake overhead on every probe.

#### `_unhealthy(reason="") → Dict`

Returns standardized unhealthy dict:
```python
{"status": "unhealthy", "load": 100.0, "response_time_ms": 0.0, "last_check": "<now>", "reason": reason}
```
`load=100.0` ensures unhealthy servers sort to the end on the load tiebreaker.

#### `check_agent_health(health_url) → Dict` (async)

1. GET `health_url`, measure round-trip in ms
2. HTTP status >= 400 → `_unhealthy(f"HTTP {status_code}")`
3. Parse JSON body, extract `load_percent` or `load` field (default 50.0 on parse failure)
4. Status logic:
   - `load >= 90` OR `elapsed > SLOW_MS` → `"degraded"`
   - Otherwise → `"healthy"`

Exception mapping: `ConnectError` → unhealthy("connection refused"), `TimeoutException` → unhealthy("timeout"), any other → unhealthy(str(exc)[:80]).

#### `probe_endpoint(endpoint) → Dict` (async)

Auto-discovery for endpoints registered without a `health_check_url`. Tries in order:
1. `{endpoint}/.well-known/agent.json` — A2A AgentCard standard
2. `{endpoint}/health` — REST convention
3. `{endpoint}/healthz` — Kubernetes convention

Returns first successful result. Falls back to `_unhealthy("all probe URLs failed")`.

---

### 5.4 `server_selection.py` — Ranking Engine

**File:** `agentns/server_selection.py`  
**Purpose:** Pure ranking functions. No I/O, no state. Deterministic given the same inputs.

#### `CITY_COORDS: Dict[str, Tuple[float, float]]`

160+ cities → `(latitude, longitude)`. Used to inject coordinates when an agent registers with just a city name, and to resolve requester location from context.

#### `_haversine(lat1, lon1, lat2, lon2) → float`

Great-circle distance in km using the Haversine formula. R = 6371.0 km. Accurate to < 0.5% for any two points on Earth.

#### `rank_servers(servers, health_map, ctx, geo_policy=None) → List[Tuple]`

Core ranking function. Returns `[(server_dict, health_dict), ...]` sorted best-first.

Sort key per server:
```python
(
    _health_score(status),          # 0=healthy → 3=unhealthy (excluded)
    proto_score,                    # 0=preferred protocol available, 1=not
    _geo_distance(server, latlon),  # km, inf if no location data
    health.get("response_time_ms"), # ms, 9999 if unknown
    health.get("load"),             # 0–100%, 50 if not reported
)
```

Unhealthy endpoints are excluded entirely (not just ranked last) unless `include_unhealthy=True`.

#### `select_protocol(server_protocols, preferred) → str`

Returns first protocol from `preferred` that exists in `server_protocols` (case-insensitive). Falls back to `server_protocols[0]`, then `"http"`.

#### `calculate_ttl(health) → int`

| Status | TTL | Rationale |
|--------|-----|-----------|
| healthy | 60s | Stable |
| degraded | 15s | Recheck soon |
| unknown | 10s | No data yet |
| unhealthy | 5s | Emergency fallback — recheck almost immediately |

---

### 5.5 `geo_policy.py` — Geo Routing Policies

**File:** `agentns/geo_policy.py`  
**Purpose:** Pluggable strategies for the geographic component of server selection.

```python
from agentns.geo_policy import NearestPolicy, LeastLoadedPolicy, CompositePolicy
from agentns.server_selection import rank_servers

# Distance only (ignores load and latency for geo component):
ranked = rank_servers(servers, health_map, ctx, geo_policy=NearestPolicy())

# Load only:
ranked = rank_servers(servers, health_map, ctx, geo_policy=LeastLoadedPolicy())

# Default composite (distance + RTT + load weighted sum):
ranked = rank_servers(servers, health_map, ctx)
```

The `CompositePolicy` weights:
```
score = (geo_weight × distance_km) + (rtt_weight × response_ms) + (load_weight × load%)
```

Default weights: `geo=0.5, rtt=0.3, load=0.2`. Override via subclass.

---

### 5.6 `geocoder.py` — City Resolution

**File:** `agentns/geocoder.py`  
**Purpose:** Resolve city names to lat/lon coordinates. CITY_COORDS provides 160+ built-in cities. Unknown cities fall back to OpenStreetMap Nominatim (free, no API key required).

Nominatim calls are cached in-process to avoid repeated network lookups. Disable with `AGENTNS_GEOCODING=off`.

---

### 5.7 `cache.py` — Resolution Cache

**File:** `agentns/cache.py`  
**Purpose:** asyncio-safe, TTL-based in-memory cache for resolved agent responses.

#### `ResolutionCache`

Internal state:
```python
_store:  Dict[str, Tuple[Any, float]]  # key → (payload, expiry_monotonic)
_lock:   asyncio.Lock
_hits:   int
_misses: int
```

#### `make_key(agent_name, requester_context) → str`

Deterministic MD5 hex digest over:
1. agent label
2. `sorted(protocols)` — order-independent
3. `json.dumps(location, sort_keys=True)` — key-order-independent

MD5 used for speed (not security). Collision-resistant enough for this key space.

#### `get(key) → Optional[Any]` (async)

Checks `time.monotonic() > expiry`. Uses monotonic clock — immune to NTP/DST wall-clock jumps.

#### `set(key, payload, ttl) → None` (async)

Stores `(payload, monotonic() + ttl)`.

#### `purge_expired() → int` (async)

Evicts all expired entries. Called by `_health_loop()` every sweep to bound memory growth.

#### `invalidate(agent_name) → int` (async)

Removes cache entries tagged with `_cache_key_agent == agent_name`. Used on deregistration.

---

### 5.8 `urn_parser.py` — URN Parsing

**File:** `agentns/urn_parser.py`  
**Purpose:** Parse, build, and validate Agent URNs. No I/O, no external dependencies.

#### URN Format

```
urn : <tld> : <namespace> : <label>

urn:acme.com:sales:emailer
 │       │       │       └── label     — agent role ("emailer", "alerts")
 │       │       └────────── namespace — application/org grouping ("sales", "acme")
 │       └────────────────── tld       — top-level domain ("agentns.local", "agents.example.com")
 └────────────────────────── literal scheme prefix "urn"
```

#### `parse_urn(value) → ParsedURN`

Never raises. Handles all input forms:

| Input | tld | namespace | label |
|-------|-----|-----------|-------|
| `"urn:acme.com:sales:emailer"` | `acme.com` | `sales` | `emailer` |
| `"urn:agentns.local:emailer"` | `agentns.local` | `""` | `emailer` |
| `"emailer"` | `""` | `""` | `emailer` |

#### `build_urn(tld, namespace, label) → str`

Constructs `f"urn:{tld}:{namespace}:{label}"`. Used to set `agent_name` on every registered endpoint.

#### `extract_label(value) → str`

Shortcut: `parse_urn(value).label`. Returns original string if label is empty.

---

### 5.9 `registry_adapter.py` — Pluggable Backends

**File:** `agentns/registry_adapter.py`  
**Purpose:** Abstract interface for custom registry backends. Allows agentns to delegate resolution to an external service.

#### Provided Adapters

| Class | Description |
|-------|-------------|
| `HttpRegistryAdapter` | Forwards resolve calls to any HTTP registry endpoint |
| `StaticRegistryAdapter` | In-memory dict or YAML file — for static environments |
| `MultiRegistryAdapter` | Fan-out: tries multiple registries in order (primary + fallback) |
| `RegistryAdapter` (ABC) | Base class — implement `resolve()` and `health()` |

#### Custom Adapter Example

```python
from agentns.registry_adapter import RegistryAdapter
import httpx

class ConsulAdapter(RegistryAdapter):
    async def resolve(self, agent_path, requester_context):
        label = agent_path.split(":")[-1]
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://consul:8500/v1/health/service/{label}?passing=true")
        services = r.json()
        if not services:
            return None
        svc = services[0]["Service"]
        return {"endpoint": f"http://{svc['Address']}:{svc['Port']}", "protocol": "A2A", "ttl": 30}

    async def health(self):
        return {"status": "ok"}
```

#### Activation

```bash
REGISTRY_ADAPTER=static  REGISTRY_YAML=/etc/agentns/agents.yaml  agentns-server
REGISTRY_ADAPTER=multi   REGISTRY_URLS=http://primary:6900,http://backup:6900  agentns-server
```

---

### 5.10 `auth.py` — Security Headers

**File:** `agentns/auth.py`  
**Purpose:** Security headers middleware. No API key authentication — agentns is designed for sidecar/internal network deployments.

#### `security_headers_middleware(request, call_next) → Response`

Injects security headers on every response:

```
X-Content-Type-Options: nosniff
X-Frame-Options:        DENY
X-XSS-Protection:       1; mode=block
Referrer-Policy:        no-referrer
Cache-Control:          no-store
```

Registered via `app.middleware("http")` in `server.py`.

**Note:** API key authentication was intentionally removed. agentns assumes it runs on an internal network (sidecar, VPC, Kubernetes cluster). For public deployments, put it behind a reverse proxy (nginx, Traefik, API Gateway) that handles authentication at the edge.

---

### 5.11 `target_lib.py` — Agent Registration SDK

**File:** `agentns/target_lib.py`  
**Purpose:** Python SDK for agents that want to register themselves.

```python
import agentns

# Connect (reads AGENTNS_URL from env)
client = agentns.target_lib.connect()

# Register at startup
await client.record(agentns.DeploymentSpec(
    leaf_name  = "alerts",
    a2a_url    = "http://myhost:9001",
    health_url = "http://myhost:9001/health",
    region     = "us-east",
    location   = {"city": "Boston"},
    protocols  = ["A2A"],
))

# Deregister at shutdown
await client.deregister("alerts", "http://myhost:9001")
```

`record()` retries automatically (default 3 attempts, 2s backoff). Safe to call at startup before agentns is fully ready.

`deregister()` uses a URL query parameter (`?endpoint=...`) instead of a request body so it works through cloud HTTP proxies that strip DELETE request bodies.

---

### 5.12 `requester_lib.py` — Agent Resolution SDK

**File:** `agentns/requester_lib.py`  
**Purpose:** Python SDK for agents that want to resolve and call other agents.

```python
import agentns

# Connect (reads AGENTNS_URL from env)
client = agentns.requester_lib.connect()

# Resolve to a URL
endpoint = await client.resolve(agentns.Query.from_label("alerts"))
if endpoint:
    resp = await httpx.AsyncClient().post(endpoint.url, json={...})

# Or with context
endpoint = await client.resolve(agentns.Query(
    agent_name        = agentns.AgentName.from_label("alerts"),
    requester_context = agentns.RequesterContext(
        location  = {"city": "Boston"},
        protocols = ["A2A"],
    ),
))
```

`resolve()` never raises — returns `None` on any failure so callers implement their own fallback.

---

## 6. Data Models

### Endpoint Entry (in `_registry`)

```python
{
    "endpoint":         "http://ny-host:9001",
    "health_check_url": "http://ny-host:9001/health",  # empty if auto-discover
    "namespace":        "agents.local",
    "protocols":        ["A2A"],
    "region":           "us-east",
    "region_label":     "New York, NY",
    "flag":             "🇺🇸",
    "location": {
        "city":         "New York",
        "latitude":     40.7128,
        "longitude":    -74.0060
    },
    "agent_name":       "urn:agentns.local:agents.local:emailer"
}
```

### Health Dict (in `_health_cache`)

```python
{
    "status":           "healthy",   # "healthy" | "degraded" | "unhealthy" | "unknown"
    "load":             42.5,        # 0–100, default 50 if not reported
    "response_time_ms": 87.3,        # round-trip to health URL in ms
    "last_check":       "2025-04-17T14:23:01.456789+00:00",
    "reason":           ""           # populated only on unhealthy
}
```

### Resolution Response (from `POST /resolve`)

```python
{
    "endpoint":             "http://lon-host:9001",
    "url":                  "http://lon-host:9001",    # alias for endpoint
    "protocol":             "A2A",
    "ttl":                  60,
    "region":               "London, UK",
    "flag":                 "🇬🇧",
    "cached":               False,
    "selected_by":          "geo_nearest",
    "resolution_time_ms":   3.7,
    "metadata": {
        "label":            "emailer",
        "latency_ms":       210.0,
        "total_candidates": 2,
        "all_candidates": [
            {"endpoint": "http://lon-host:9001", "status": "healthy", "latency_ms": 210.0},
            {"endpoint": "http://ny-host:9001",  "status": "healthy", "latency_ms": 45.0}
        ]
    }
}
```

### MongoDB Document Schema

```javascript
{
    "_id":               ObjectId("..."),
    "label":             "emailer",              // indexed
    "endpoint":          "http://ny-host:9001",  // part of unique compound index
    "health_check_url":  "http://ny-host:9001/health",
    "namespace":         "agents.local",
    "protocols":         ["A2A"],
    "region":            "us-east",
    "region_label":      "New York, NY",
    "flag":              "🇺🇸",
    "location":          {"city": "New York", "latitude": 40.7128, "longitude": -74.006},
    "agent_name":        "urn:agentns.local:agents.local:emailer",
    "registered_at":     ISODate("2025-04-17T10:00:00Z"),  // $setOnInsert — never changes
    "last_seen":         ISODate("2025-04-17T14:23:01Z")   // updated on every re-registration
}
```

---

## 7. Server Selection Algorithm

The ranking algorithm answers: *"Given N endpoints for label X, and a request from location L wanting protocol P, which endpoint should answer?"*

### Sort Key

```python
(health_score, protocol_score, geo_distance_km, response_time_ms, load_percent)
```

Each position is only consulted when all positions to its left are tied — strict priority hierarchy.

### Priority 1: Health

```
healthy (0) >> degraded (1) >> unknown (2) >> unhealthy (excluded)
```

### Priority 2: Protocol Compatibility

```
preferred protocol available (0) >> not available (1)
```

### Priority 3: Geographic Distance

```
haversine(requester, server) in km
math.inf if no location data → falls through to latency
```

### Priority 4: Response Time

```
health.get("response_time_ms", 9999.0) — lower is better
```

### Priority 5: CPU Load

```
health.get("load", 50.0) — lower is better (0–100%)
```

### Worked Example

Two replicas, requester in Boston:

```
               NYC (45ms, 30% load)     London (210ms, 20% load)
health_score:   0 (healthy)              0 (healthy)
proto_score:    0 (A2A ✓)               0 (A2A ✓)
geo_km:         306 km                  5,263 km
latency_ms:     45 ms                   210 ms
load:           30%                     20%

Sort keys:
  NYC:    (0, 0,  306, 45,  30)
  London: (0, 0, 5263, 210, 20)

NYC wins at position 3 (geo_km 306 < 5263)
selected_by = "geo_nearest"
```

Without location:
```
  NYC:    (0, 0, inf, 45,  30)
  London: (0, 0, inf, 210, 20)

NYC wins at position 4 (latency_ms 45 < 210)
selected_by = "lowest_latency"
```

---

## 8. MongoDB Integration

### Overview

MongoDB provides **optional persistence** — the registry survives process restarts without requiring all agents to re-register. It is not required for operation.

### Connection

Set `MONGODB_URI` to any valid MongoDB connection string:

```bash
# Local MongoDB
MONGODB_URI=mongodb://localhost:27017/

# MongoDB Atlas (cloud)
MONGODB_URI="mongodb+srv://user:pass@cluster0.abc.mongodb.net/"

# Local with auth
MONGODB_URI="mongodb://agentns:secret@mongo:27017/agentns"
```

Leave unset for in-memory-only mode.

### Indexes Created at Startup

```javascript
// Fast lookups by agent name
db.agents.createIndex({ "label": 1 })

// Prevent duplicate registrations, enables upsert semantics
db.agents.createIndex({ "label": 1, "endpoint": 1 }, { unique: true })
```

### Upsert Semantics

Every `POST /register` call performs:

```python
await _mongo_col.update_one(
    {"label": label, "endpoint": entry["endpoint"]},   # unique identity
    {
        "$set":         {**entry_doc, "last_seen": now},   # always update
        "$setOnInsert": {"registered_at": now},             # only on first insert
    },
    upsert=True,
)
```

Calling `/register` 100 times with the same `(label, endpoint)` produces **exactly one document**. `registered_at` is set on creation and never changes. `last_seen` is updated on every call.

### Failure Handling

| Failure scenario | Behavior |
|-----------------|----------|
| MongoDB unreachable at startup | Log warning, continue in-memory mode |
| MongoDB write fails during /register | Log error, registration still succeeds in-memory |
| MongoDB load fails at startup | Log error, start with empty registry |
| MongoDB disconnects at runtime | In-memory registry continues; writes logged as errors |

MongoDB failures **never** prevent startup or cause the server to stop accepting requests.

### What Persists vs. What Doesn't

| Persisted in MongoDB | Not persisted |
|---------------------|---------------|
| Endpoint registrations (all fields) | Health check results |
| registration_at / last_seen timestamps | Resolution cache |
| Namespace, region, location metadata | Background task state |

Health data is intentionally not persisted — it's ephemeral and becomes stale immediately. The initial health sweep on startup gets fresh data within seconds.

### Docker Compose with Local MongoDB

```bash
# Start agentns + local MongoDB together
docker compose --profile mongo up

# With MongoDB Atlas
MONGODB_URI="mongodb+srv://..." docker compose up
```

---

## 9. Concurrency Model

agentns uses **cooperative multitasking** via Python's asyncio. No threads.

### Event Loop Structure

```
asyncio event loop
    ├── uvicorn ASGI server        (handles HTTP connections)
    │     ├── proxy_agent()        (per /proxy/* request)
    │     ├── resolve()            (per /resolve request)
    │     ├── register()           (per /register request)
    │     └── health() / agents()  (per monitoring request)
    │
    └── _health_loop() task        (background, started in lifespan)
          └── _check_all()         (asyncio.gather — all probes parallel)
```

### Locking

| State | Lock | Reason |
|-------|------|--------|
| `_health_cache` | `_health_lock` (asyncio.Lock) | Written by both background loop and resolve() inline checks |
| `_registry` | None needed | Only mutated in register()/deregister() — no concurrent writes |
| `_cache` | Internal lock in ResolutionCache | get/set/purge from multiple coroutines |

### Health Probe Parallelism

```python
await asyncio.gather(*[_check_one(u, h) for u, h in seen.items()], return_exceptions=True)
```

For 50 endpoints with 5s timeout each: sequential = 250s, parallel = 5s max. `return_exceptions=True` — one timed-out probe does not cancel the others.

---

## 10. API Reference

### `ANY /proxy/{label}` and `ANY /proxy/{label}/{path}`

Forward any request to the best healthy endpoint registered under `label`.

```bash
# Forward a message to the "alerts" agent:
curl -X POST http://localhost:8200/proxy/alerts \
  -H "Content-Type: application/json" \
  -d '{"method": "message/send", "params": {"text": "hello"}}'

# Forward with a sub-path:
curl http://localhost:8200/proxy/alerts/tasks/list

# Fetch A2A agent card (url field auto-rewritten to proxy URL):
curl http://localhost:8200/proxy/alerts/.well-known/agent.json
```

All HTTP methods supported. SSE streaming works transparently.

**Responses:**
- `200` — upstream responded
- `404` — label not registered
- `502` — upstream connection refused
- `504` — upstream timed out

---

### `POST /register`

Register or update an agent endpoint.

**Request body:**
```json
{
  "label":            "emailer",
  "endpoint":         "http://host:9001",
  "namespace":        "acme.sales",
  "region":           "us-east",
  "location":         {"city": "New York"},
  "protocols":        ["A2A"],
  "health_check_url": "http://host:9001/health",
  "flag":             "🇺🇸"
}
```

Only `label` and `endpoint` are required.

**Response 200:**
```json
{
  "status":          "registered",
  "label":           "emailer",
  "endpoint":        "http://host:9001",
  "agent_name":      "urn:agentns.local:acme.sales:emailer",
  "total_endpoints": 1,
  "geo_routing":     "active"
}
```

`"status"` is `"updated"` if the endpoint was already registered (idempotent).  
**Response 400:** `label` or `endpoint` missing.

---

### `POST /resolve`

Resolve an agent label or URN to its best available endpoint URL.

**Request body:**
```json
{
  "label": "emailer",
  "requester_context": {
    "location":  {"city": "Boston"},
    "protocols": ["A2A", "http"]
  }
}
```

Also accepts `"agent_name"` (URN) in place of `"label"`.

**Response 200:**
```json
{
  "endpoint":           "http://host:9001",
  "url":                "http://host:9001",
  "protocol":           "A2A",
  "ttl":                60,
  "region":             "New York, NY",
  "flag":               "🇺🇸",
  "cached":             false,
  "selected_by":        "geo_nearest",
  "resolution_time_ms": 3.7,
  "metadata": {
    "label":            "emailer",
    "latency_ms":       45.0,
    "total_candidates": 2,
    "all_candidates":   [...]
  }
}
```

**Response 400:** No `agent_name` or `label` provided.  
**Response 404:** Label not registered, or URN TLD has no registered remote registry.

---

### `DELETE /register/{label}`

Remove one or all endpoints for a label.

```bash
# Remove a specific endpoint (body):
curl -X DELETE http://localhost:8200/register/emailer \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "http://host:9001"}'

# Remove a specific endpoint (query param, cloud-proxy safe):
curl -X DELETE "http://localhost:8200/register/emailer?endpoint=http://host:9001"

# Remove all endpoints for label:
curl -X DELETE http://localhost:8200/register/emailer
```

**Response 200:**
```json
{"status": "deregistered", "label": "emailer", "removed": 1}
```

---

### `GET /switchboard/registries`

List all connected registries (local + all remotes).

```json
{
  "local": {
    "tld": "mbta.local",
    "namespace": "transit",
    "total_labels": 5,
    "total_endpoints": 8
  },
  "remotes": [
    {
      "tld": "hospital.local",
      "url": "http://hospital-agentns:8200",
      "registry_id": "hospital.local",
      "status": "reachable",
      "added_at": "2025-05-07T10:00:00Z"
    }
  ],
  "count": 1
}
```

---

### `POST /switchboard/registries`

Register a remote registry at runtime.

**Request body:**
```json
{
  "tld":  "hospital.local",
  "url":  "http://hospital-agentns:8200"
}
```

Both fields are required. agentns probes the remote's `/health` endpoint to confirm it is reachable.

**Response 200:**
```json
{"status": "registered", "tld": "hospital.local", "url": "http://hospital-agentns:8200", "reachable": true}
```

**Response 400:** `tld` or `url` missing.

---

### `DELETE /switchboard/registries/{tld}`

Remove a remote registry.

```bash
curl -X DELETE http://localhost:8200/switchboard/registries/hospital.local
```

**Response 200:**
```json
{"status": "removed", "tld": "hospital.local"}
```

**Response 404:** TLD not found in federation table.

---

### `GET /health`

Full server health report. Always returns HTTP 200.

```json
{
  "ok": true,
  "status": "healthy",
  "version": "3.0.0",
  "total_labels": 3,
  "total_endpoints": 5,
  "mongodb": "connected",
  "proxy": {
    "enabled": true,
    "mode": "agentgateway",
    "endpoint": "http://agentgateway:8400",
    "slim_org": ""
  },
  "switchboard": {
    "enabled": true,
    "remote_registries": 2,
    "tlds": ["hospital.local", "payments.local"]
  },
  "agents": {
    "emailer": [
      {"endpoint": "http://host:9001", "status": "healthy", "latency_ms": 45.0}
    ]
  }
}
```

---

### `GET /agents`

All registered labels with per-endpoint health status. Designed for dashboards.

### `GET /namespaces`

All namespaces and the labels in each:
```json
{"tld": "agentns.local", "namespaces": {"agents.local": ["emailer", "alerts"]}}
```

### `GET /cache/stats`

Cache hit rate and entry counts:
```json
{"hits": 42, "misses": 8, "hit_rate_pct": 84.0, "active_entries": 5, "expired_entries": 0}
```

### `POST /cache/clear`

Flush all cached resolutions:
```json
{"status": "cleared", "removed": 5}
```

---

## 11. Configuration Reference

All configuration is via environment variables. No config files, no hardcoded values.

### Server Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTNS_PORT` | `8200` | HTTP port |
| `AGENTNS_NAMESPACE` | `agents.local` | Default URN namespace for new registrations |
| `AGENTNS_TLD` | `agentns.local` | URN TLD this instance owns (e.g. `mbta.local`) |
| `AGENTNS_HEALTH_INTERVAL` | `30` | Seconds between background health sweeps |
| `AGENTNS_GEOCODING` | `on` | Set `off` to disable Nominatim geocoding |

### Federation / Switchboard Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FEDERATION_REGISTRIES` | *(none)* | Remote registries to wire at startup. JSON `{"tld":"url"}` or CSV `tld=url,tld=url` |

**JSON format example:**
```bash
FEDERATION_REGISTRIES='{"hospital.local":"http://hospital:8200","payments.local":"http://payments:8200"}'
```

**CSV format example:**
```bash
FEDERATION_REGISTRIES=hospital.local=http://hospital:8200,payments.local=http://payments:8200
```

Remote registries can also be added/removed at runtime via `POST/DELETE /switchboard/registries` without restarting.

### MongoDB Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGODB_URI` | *(none)* | MongoDB connection string. Empty = in-memory mode |
| `MONGODB_DB` | `agentns` | MongoDB database name |

### Proxy Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTNS_PROXY_HOST` | *(none)* | Optional external proxy hostname (e.g., Agentgateway) |
| `AGENTNS_PROXY_PORT` | `8400` | External proxy port |
| `AGENTNS_PROXY_MODE` | `agentgateway` | `agentgateway` or `custom` |
| `A2A_PROXY_ENDPOINTS` | *(none)* | Explicit comma-separated proxy base URLs (overrides HOST+PORT) |
| `SLIM_ORG` | *(none)* | SLIM org prefix for `slim_identity` |

### Registry Adapter Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REGISTRY_ADAPTER` | `http` | `http` / `static` / `multi` |
| `REGISTRY_URL` | `http://localhost:6900` | HTTP registry URL |
| `REGISTRY_YAML` | `agents.yaml` | Static registry YAML path |
| `REGISTRY_URLS` | *(none)* | Comma-separated URLs for multi-adapter |

### Client Variables (used by target_lib / requester_lib)

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTNS_URL` | `http://localhost:8200` | Server URL used by both client SDKs |
| `AGENTNS_RESOLVER_URL` | *(AGENTNS_URL)* | Override resolver URL separately |
| `ANS_TLD` | `agentns.local` | TLD for `AgentName.from_label()` |
| `ANS_APP` | `default` | Namespace for `AgentName.from_label()` |

---

## 12. Error Handling

### Principle: Never Crash the Caller

Every code path returns a response rather than propagating an unhandled exception.

| Scenario | Behavior |
|----------|----------|
| MongoDB unreachable at startup | Log error, continue in-memory |
| MongoDB write fails during /register | Log error, registration succeeds in-memory |
| MongoDB load fails at startup | Log error, start with empty registry |
| Health probe times out | `_unhealthy("timeout")` |
| Health probe connection refused | `_unhealthy("connection refused")` |
| All endpoints unhealthy during resolve | `emergency_fallback` with TTL=5, HTTP 200 |
| One probe in gather() throws | `return_exceptions=True` — others continue |
| Background health loop iteration fails | Log warning, sleep, retry |
| Proxy upstream connection refused | HTTP 502 Bad Gateway |
| Proxy upstream timed out | HTTP 504 Gateway Timeout |
| `requester_lib.resolve()` throws | Catches all, returns `None` |
| Unknown status in `_health_score()` | Defaults to 2 (unknown) |
| City not in CITY_COORDS | Returns `None`, disables geo ranking |

### HTTP Status Codes

| Code | When |
|------|------|
| 200 | All success cases, including emergency_fallback |
| 400 | Missing required fields |
| 404 | Label not registered; URN TLD has no registered federation remote |
| 502 | Proxy: upstream refused connection |
| 504 | Proxy: upstream timed out |

The 200 for emergency fallback is intentional — returning 503 would cause orchestrators to fail hard. A 200 with TTL=5 lets the caller try the endpoint (which may be partially functional) and retry resolution in 5 seconds.

---

## 13. Performance Characteristics

### Resolution Latency

| Path | Typical |
|------|---------|
| Cache hit | < 1 ms |
| Cache miss, all endpoints in health cache | 1–5 ms |
| Cache miss, one endpoint not yet checked (live probe) | 50–500 ms |

### Health Sweep

All probes run concurrently via `asyncio.gather()`. For N endpoints:
- Sequential: `N × avg_probe_latency`
- With gather: `≈ max_probe_latency`

For 50 endpoints at 100ms each: 100ms sweep vs 5000ms sequential.

### Proxy Throughput

The proxy adds one httpx async round-trip. For in-datacenter requests: ~1–5ms added latency over direct calls. SSE streaming has no additional buffering — chunks are yielded as received.

### Memory

- `_registry`: ~1–5 KB per endpoint
- `_health_cache`: ~500 bytes per endpoint
- `ResolutionCache._store`: 1–5 KB per distinct (label + context) combination

For 100 agents × 500 resolution combinations: total in-memory state < 10 MB.

---

## 14. Deployment Guide

### Minimum (in-memory, no persistence)

```bash
pip install agentns
agentns-server
# or
docker run -p 8200:8200 ghcr.io/tonystark3110/agentns:latest
```

Registry is lost on restart. Agents must re-register. Fine for development and single-process setups.

### Production (MongoDB Atlas)

```bash
docker run -d --restart always \
  -p 8200:8200 \
  -e MONGODB_URI="mongodb+srv://user:pass@cluster0.abc.mongodb.net/" \
  ghcr.io/tonystark3110/agentns:latest
```

Open port 8200 in your firewall/security group. All agents point `AGENTNS_URL` at this server.

### Full Stack (Docker Compose + local MongoDB)

```bash
# Clone repo
git clone https://github.com/tonystark3110/agentns && cd agentns

# Development (in-memory):
docker compose up

# With local MongoDB (persistent):
docker compose --profile mongo up

# Production overrides (resource limits, logging, healthcheck):
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile mongo up -d
```

### Cloud Workflow for a Multi-Agent System

**Step 1 — Deploy agentns** (once, on any server):

```bash
# EC2, GCP VM, Azure VM — pick one
export MONGODB_URI="mongodb+srv://user:pass@cluster.mongodb.net/"
docker run -d --restart always -p 8200:8200 \
  -e MONGODB_URI="$MONGODB_URI" \
  ghcr.io/tonystark3110/agentns:latest
# Note your server's public IP, open port 8200
```

**Step 2 — Configure every agent**:

```bash
# Set on each agent's server/container/Lambda
AGENTNS_URL=http://your-agentns-server-ip:8200
```

**Step 3 — Register at agent startup**:

```python
import agentns, os

client = agentns.target_lib.connect()
await client.record(agentns.DeploymentSpec(
    leaf_name  = "alerts",
    a2a_url    = f"http://{os.environ['MY_PUBLIC_IP']}:9001",
    health_url = f"http://{os.environ['MY_PUBLIC_IP']}:9001/health",
    region     = "us-east",
    location   = {"city": "Boston"},
    protocols  = ["A2A"],
))
```

**Step 4a — Call through the proxy** (simplest, recommended):

```python
import httpx, os

resp = await httpx.AsyncClient().post(
    f"http://{os.environ['AGENTNS_URL']}/proxy/alerts",
    json={"method": "message/send", "params": {"text": "hello"}}
)
```

No need to know the real endpoint. agentns handles health-aware routing automatically.

**Step 4b — Resolve first, then call directly** (lower latency for high-frequency calls):

```python
import agentns

client   = agentns.requester_lib.connect()
endpoint = await client.resolve(agentns.Query.from_label("alerts"))
if endpoint:
    resp = await httpx.AsyncClient().post(endpoint.url, json={...})
```

Cache hit latency < 1 ms. Use this pattern for hot paths.

---

## 15. Development Guide

### Setup

```bash
git clone https://github.com/tonystark3110/agentns
cd agentns
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest tests/ -v
pytest tests/ -v --tb=short   # compact output
pytest tests/test_api.py -k "proxy"  # filter by name
```

Tests use `httpx.AsyncClient(transport=ASGITransport(app=app))` — no real network, no external services needed.

### Test Structure

```
tests/
├── conftest.py          — pytest config (minimal)
└── test_api.py          — integration tests for all HTTP endpoints
```

Each test:
1. Clears `_registry`, `_health_cache`, `_cache` via the `clear_state` autouse fixture
2. Gets a fresh `client` fixture (in-process ASGI transport)
3. Makes HTTP calls against the real FastAPI app

### Adding Tests

```python
@pytest.mark.asyncio
async def test_my_feature(client):
    # Register an endpoint
    await client.post("/register", json={"label": "myagent", "endpoint": "http://host:9001"})

    # Inject health status (avoids real network calls in tests)
    _health_cache["http://host:9001"] = {
        "status": "healthy", "load": 10.0, "response_time_ms": 20.0, "last_check": "now"
    }

    # Test your feature
    resp = await client.post("/resolve", json={"label": "myagent"})
    assert resp.status_code == 200
    assert resp.json()["endpoint"] == "http://host:9001"
```

### GitHub Actions / CI

`.github/workflows/docker-publish.yml` builds and pushes to GHCR on every push to `main`.

Required steps:
1. `docker/setup-buildx-action@v3` — required by `build-push-action@v5`
2. `docker/login-action@v3` — authenticates to GHCR using `GITHUB_TOKEN`
3. `docker/build-push-action@v5` — builds multi-platform image

### Release Checklist

1. Bump version in `pyproject.toml` and `server.py` (`__version__`)
2. Update `TECHNICAL.md` version number
3. Run `pytest tests/ -v` — all must pass
4. `git tag v3.x.x && git push origin v3.x.x`
5. GitHub Actions builds and pushes `ghcr.io/tonystark3110/agentns:v3.x.x` and `:latest`

---

## 16. Switchboard / Federation

### Overview

Each agentns instance **owns a TLD**. When a `/resolve` request arrives for a URN whose TLD belongs to a different registry, the request is automatically forwarded to the correct remote instance — transparent to the caller.

```
Client                     agentns A (mbta.local)        agentns B (hospital.local)
──────                     ───────────────────────        ──────────────────────────
POST /resolve
{"agent_name":
 "urn:hospital.local
  :er:triage"}
                      ─────────────────────────►
                           TLD = hospital.local
                           Not owned locally
                           → forward to remote B
                                                    ─────────────────────────────────►
                                                          local resolve
                                                    ◄─────────────────────────────────
                           tag: federated_from=B
                      ◄─────────────────────────
         {
           "endpoint": "http://er-triage:9001",
           "federated_from": "http://hospital-agentns:8200"
         }
```

### Data Structures

```python
_federation: Dict[str, Dict] = {}
```

Module-level dict. Maps TLD → remote registry metadata:

```python
_federation["hospital.local"] = {
    "url":         "http://hospital-agentns:8200",
    "registry_id": "hospital.local",
    "status":      "reachable",
    "added_at":    "2025-05-07T10:00:00Z",
}
```

### Startup Loading

`_load_federation_from_env()` is called at import time. It reads `FEDERATION_REGISTRIES` and populates `_federation` before the first HTTP request arrives.

```python
# Supported formats:
# JSON:
FEDERATION_REGISTRIES='{"hospital.local":"http://hospital:8200"}'

# CSV:
FEDERATION_REGISTRIES=hospital.local=http://hospital:8200,payments.local=http://payments:8200
```

### Resolve Routing Logic

In `POST /resolve`, after parsing the URN:

```python
if parsed.tld and parsed.tld != DEFAULT_TLD:
    remote = _federation.get(parsed.tld)
    if remote:
        # Proxy request to the registry that owns this TLD
        return await _federated_resolve(remote["url"], body)
    # No remote owns this TLD → 404
    raise HTTPException(404, f"No registry is registered for TLD '{parsed.tld}'...")
```

**Key behaviour:**
- TLD matches local `AGENTNS_TLD` → resolve locally (any namespace valid)
- TLD found in `_federation` → forward to that remote transparently
- TLD unknown → `404 No registry registered for TLD '...'`
- Namespace is **never validated** — multiple namespaces are valid within one registry

### `_federated_resolve(remote_url, body)` (async)

Shared `_proxy_client` (created in lifespan, closed on shutdown) posts the request body to `{remote_url}/resolve`. The result is tagged with `"federated_from": remote_url` before returning to the caller.

Using a shared client eliminates TCP/TLS overhead — no new connection is established per proxy hop.

### Switchboard Endpoints

#### `GET /switchboard/registries`

Lists the local registry summary plus all entries in `_federation`.

#### `POST /switchboard/registries`

Adds a new remote. Probes `{url}/health` before accepting the registration. Returns `"reachable": true/false` based on probe result. Does **not** reject the registration if unreachable — the remote may come up later.

#### `DELETE /switchboard/registries/{tld}`

Removes a TLD entry from `_federation`. Subsequent resolves for that TLD will return 404.

### Multi-Registry Deployment Example

```
                    ┌─────────────────────────────────┐
                    │   Central Gateway                │
                    │   AGENTNS_TLD=gateway.local      │
                    │                                  │
                    │   FEDERATION_REGISTRIES=         │
                    │   mbta.local=http://mbta:8200,   │
                    │   hospital.local=http://hosp:8200│
                    └──────────────┬──────────────────┘
                                   │ forwards based on TLD
                    ┌──────────────┴──────────────────┐
                    │                                  │
          ┌─────────┴──────────┐         ┌────────────┴───────────┐
          │  agentns (MBTA)    │         │  agentns (Hospital)    │
          │  AGENTNS_TLD=      │         │  AGENTNS_TLD=          │
          │    mbta.local      │         │    hospital.local      │
          │  agents: rider,    │         │  agents: triage,       │
          │    planner, alerts │         │    lab, pharmacy       │
          └────────────────────┘         └────────────────────────┘
```

Each team runs their own agentns with their own TLD. Agents from any team can resolve agents in any other team using the full URN — the gateway instance forwards transparently.

---

*Technical Reference — agentns v3.0.0 — MIT License*
