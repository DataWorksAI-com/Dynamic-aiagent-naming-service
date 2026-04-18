# agentns — Agent Name Service

> **DNS for AI agents.** A single-binary sidecar that gives every agent in your multi-agent system a stable name, automatic health monitoring, geographic routing, and TTL-based caching — over a plain HTTP API any language can call.

[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://python.org)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://hub.docker.com)

---

## The problem

In a multi-agent system, agents call other agents. This works fine with two agents. With ten agents deployed across clouds and regions it breaks:

- **Hardcoded URLs** break when you scale or failover
- **No health awareness** — your orchestrator blindly calls a dead agent
- **No geo-routing** — a user in Tokyo gets routed to a server in Boston
- **No fallback** — one bad endpoint takes down the whole chain

agentns solves all of this. It is the service mesh **for agents**, not services.

---

## How it works

```
Your orchestrator                agentns sidecar              Your agents
──────────────────               ───────────────              ───────────
                                                              emailer-nyc :9001  ←  register on startup
POST /resolve                    health loop (30s)            emailer-lon :9001  ←  register on startup
{"agent_name":                   ┌─────────────────┐         invoicer    :9002  ←  register on startup
 "urn:co.com:sales:emailer"}  →  │ rank by:        │
                                 │  1. health       │
{"endpoint":                  ←  │  2. geo distance │
 "http://emailer-nyc:9001",      │  3. latency      │
 "ttl": 60,                      │  4. load %       │
 "selected_by": "geo_nearest"}   └─────────────────┘
```

Three things happen automatically, with zero code changes to your agents:

1. **Background health sweep** (every 30 s) probes every registered endpoint
2. **Resolution** picks the best live endpoint for each request using health + geo + latency
3. **TTL cache** stores results so your orchestrator gets sub-millisecond responses on repeat calls

---

## Quick start

### Docker (recommended)

```bash
# Start agentns
docker run -p 8200:8200 ghcr.io/DataWorksAI-com/agentns:latest

# Register an agent
curl -X POST http://localhost:8200/register \
  -H "Content-Type: application/json" \
  -d '{"label":"emailer","endpoint":"http://my-agent:9001","region":"us-east","location":{"city":"New York"}}'

# Resolve it
curl -X POST http://localhost:8200/resolve \
  -H "Content-Type: application/json" \
  -d '{"agent_name":"urn:agentns.local:agents.local:emailer"}'
```

Response:
```json
{
  "endpoint": "http://my-agent:9001",
  "protocol": "http",
  "ttl": 60,
  "region": "New York",
  "cached": false,
  "selected_by": "only_available",
  "resolution_time_ms": 1.4
}
```

### pip

```bash
pip install agentns
agentns-server --port 8200
```

### docker-compose (with MongoDB persistence)

```bash
cp .env.example .env
# Edit .env: set MONGODB_URI if you want persistence across restarts
docker compose up
```

---

## URN format

agentns uses a hierarchical URN scheme inspired by DNS:

```
urn : <tld> : <namespace> : <label>
 │       │         │           └── agent role   (e.g. emailer, planner, alerts)
 │       │         └────────────── your app/org (e.g. sales, mbta-transit-ci)
 │       └──────────────────────── your domain  (e.g. acme.com, agents.local)
 └──────────────────────────────── literal "urn"
```

Examples:
```
urn:acme.com:sales:emailer
urn:acme.com:sales:invoicer
urn:agents.dataworksai.com:mbta-transit-ci:alerts
```

You can also resolve by short label:
```json
{"label": "emailer"}
```

---

## API reference

### `POST /register` — Register an agent endpoint

```json
{
  "label":           "emailer",
  "endpoint":        "http://my-agent:9001",
  "namespace":       "acme.sales",
  "region":          "us-east",
  "region_label":    "New York, NY",
  "location":        {"city": "New York"},
  "protocols":       ["http", "A2A"],
  "health_check_url":"http://my-agent:9001/health",
  "flag":            "🇺🇸"
}
```

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `label` | ✅ | — | Short agent name, e.g. `emailer` |
| `endpoint` | ✅ | — | Full URL the orchestrator will call |
| `namespace` | | `AGENTNS_NAMESPACE` env | URN namespace |
| `region` | | `""` | Region code, e.g. `us-east` |
| `region_label` | | same as `region` | Human-readable, e.g. `New York, NY` |
| `location` | | `{}` | `{"city":"New York"}` or `{"latitude":40.7,"longitude":-74.0}` |
| `protocols` | | `["http"]` | `A2A`, `http`, `SLIM`, etc. |
| `health_check_url` | | auto-discovered | Falls back to `/.well-known/agent.json` then `/health` |
| `flag` | | `""` | Emoji flag for UI display |

Multiple registrations for the same `label` create a **replica pool**. agentns picks the best one on each resolve.

---

### `POST /resolve` — Resolve an agent

```json
{
  "agent_name": "urn:acme.com:sales:emailer",
  "requester_context": {
    "location":  {"city": "Boston"},
    "protocols": ["A2A", "http"]
  },
  "cache_enabled": true
}
```

| Field | Notes |
|-------|-------|
| `agent_name` | Full URN, or use `label` for short form |
| `requester_context.location` | City name or lat/lon — enables geo routing |
| `requester_context.protocols` | Preferred protocol order |
| `cache_enabled` | Default `true` — set `false` to force fresh resolution |

Response:
```json
{
  "endpoint":    "http://my-agent-nyc:9001",
  "protocol":    "A2A",
  "ttl":         60,
  "region":      "New York, NY",
  "flag":        "🇺🇸",
  "cached":      false,
  "selected_by": "geo_nearest",
  "resolution_time_ms": 1.2,
  "metadata": {
    "label":            "emailer",
    "latency_ms":       42.1,
    "total_candidates": 2,
    "all_candidates": [
      {"endpoint":"http://my-agent-nyc:9001","status":"healthy","latency_ms":42,"region":"New York, NY"},
      {"endpoint":"http://my-agent-lon:9001","status":"healthy","latency_ms":218,"region":"London, UK"}
    ]
  }
}
```

`selected_by` values:
| Value | Meaning |
|-------|---------|
| `geo_nearest` | Location provided, picked closest healthy endpoint |
| `lowest_latency` | No location, picked fastest healthy endpoint |
| `only_available` | Only one healthy endpoint existed |
| `emergency_fallback` | All endpoints unhealthy — returned best guess |

---

### Other endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health + per-agent status |
| `GET` | `/agents` | All registered agents with current health |
| `GET` | `/namespaces` | All registered namespaces |
| `DELETE` | `/register/{label}` | Deregister label (body: `{"endpoint":"..."}` for specific) |
| `GET` | `/cache/stats` | Cache hit rate, entry counts |
| `POST` | `/cache/clear` | Flush the resolution cache |

---

## Python client

```python
from agentns.client import AgentNSClient

async with AgentNSClient("http://localhost:8200") as client:

    # Register on agent startup
    await client.register(
        "emailer", "http://my-host:9001",
        region="us-east",
        location={"city": "New York"},
        protocols=["http", "A2A"],
    )

    # Resolve in orchestrator
    resolved = await client.resolve(
        "urn:acme.com:sales:emailer",
        requester_context={"location": {"city": "Boston"}, "protocols": ["A2A"]},
    )

    if resolved:
        print(resolved.endpoint)        # http://my-host:9001
        print(resolved.selected_by)     # geo_nearest
        print(resolved.resolution_time_ms)  # 1.3
```

`resolved` is a `ResolvedAgent` dataclass with fields:
`endpoint`, `protocol`, `ttl`, `region`, `flag`, `cached`, `selected_by`, `resolution_time_ms`, `metadata`

---

## Configuration

All config via environment variables — zero hardcoded values.

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTNS_PORT` | `8200` | HTTP port |
| `AGENTNS_NAMESPACE` | `agents.local` | Default URN namespace |
| `AGENTNS_TLD` | `agentns.local` | Default URN TLD |
| `AGENTNS_HEALTH_INTERVAL` | `30` | Background health sweep interval (seconds) |
| `MONGODB_URI` | *(empty)* | MongoDB connection string — omit for in-memory mode |
| `MONGODB_DB` | `agentns` | MongoDB database name |
| `AGENTNS_URL` | `http://localhost:8200` | Used by `AgentNSClient()` with no args |

**In-memory mode** (no MongoDB): fast start, registrations lost on restart. Fine for local dev.

**MongoDB mode**: registrations survive restarts. All registered endpoints are reloaded and health-checked at startup.

---

## Server selection algorithm

When multiple endpoints are registered for the same label, agentns ranks them by a 5-key sort tuple:

```
(health_score, protocol_score, geo_distance_km, response_time_ms, load_percent)
```

Lower is better. The first key that differs determines the winner:

1. **Health** — `healthy=0`, `degraded=1`, `unknown=2`, `unhealthy=3`
   Unhealthy endpoints are excluded from results entirely.

2. **Protocol** — `0` if a preferred protocol is available, `1` otherwise.

3. **Geographic distance** — haversine great-circle distance from the requester's city/lat-lon.
   `∞` if no location is given (falls through to latency).

4. **Response time** — actual measured round-trip in milliseconds from the most recent health sweep.

5. **Load** — CPU/load percent reported by the agent's health endpoint.

**TTL** is calculated from health status: healthy→60s, degraded→15s, unknown→10s, unhealthy→5s.

---

## Real-world example: MBTA Transit

agentns was extracted from the **MBTA Transit AI assistant** — a production multi-agent system with:

- 4 specialist agents: `alerts`, `planner`, `stopfinder`, `fares`
- Geographic replicas: Boston (primary) + Frankfurt (failover)
- Automatic failover: Boston fares goes down → Frankfurt takes over within one health interval (30s)
- Automatic recovery: Boston comes back → it wins again on next sweep (lower latency, same geo score)

```python
# MBTA orchestrator (simplified)
from agentns.client import AgentNSClient

client = AgentNSClient("http://localhost:8200")

async def get_fares(user_city: str) -> dict:
    resolved = await client.resolve(
        "urn:agents.dataworksai.com:mbta-transit-ci:fares",
        requester_context={
            "protocols": ["A2A"],
            "location":  {"city": user_city},
        },
    )
    if not resolved:
        return {"error": "fares agent unavailable"}

    # resolved.endpoint is "http://boston-ip:8004" normally,
    # "http://frankfurt-ip:8004" when Boston is unhealthy
    return await call_agent(resolved.endpoint, {"query": "CharlieCard monthly pass"})
```

To run the full MBTA demo: `python examples/mbta_transit_example.py`

---

## Integration — any language

agentns speaks plain HTTP. No SDK required.

**Node.js:**
```js
const resolve = async (agentName, city) => {
  const res = await fetch("http://localhost:8200/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      agent_name: agentName,
      requester_context: { location: { city }, protocols: ["http"] },
    }),
  });
  return res.json();   // { endpoint, ttl, selected_by, ... }
};

const { endpoint } = await resolve("urn:acme.com:sales:emailer", "London");
await fetch(`${endpoint}/send`, { method: "POST", body: JSON.stringify(email) });
```

**Go:**
```go
type ResolveReq struct {
    AgentName        string         `json:"agent_name"`
    RequesterContext map[string]any `json:"requester_context"`
}
type ResolveResp struct {
    Endpoint   string `json:"endpoint"`
    Protocol   string `json:"protocol"`
    TTL        int    `json:"ttl"`
    SelectedBy string `json:"selected_by"`
}

func resolveAgent(agentName, city string) (*ResolveResp, error) {
    body, _ := json.Marshal(ResolveReq{
        AgentName: agentName,
        RequesterContext: map[string]any{
            "location":  map[string]string{"city": city},
            "protocols": []string{"http"},
        },
    })
    resp, err := http.Post("http://localhost:8200/resolve",
        "application/json", bytes.NewReader(body))
    if err != nil { return nil, err }
    var result ResolveResp
    json.NewDecoder(resp.Body).Decode(&result)
    return &result, nil
}
```

**Shell / CI:**
```bash
ENDPOINT=$(curl -sf http://localhost:8200/resolve \
  -d '{"label":"emailer"}' \
  -H "Content-Type: application/json" | jq -r .endpoint)

curl -X POST "$ENDPOINT/send" -d '{"to":"alice@example.com"}'
```

---

## Health check protocol

agentns probes endpoints in this order at every health sweep:

1. **Explicit `health_check_url`** — if you provided one at registration
2. **`/.well-known/agent.json`** — [A2A AgentCard](https://google.github.io/A2A/) standard
3. **`/health`** — REST convention
4. **`/healthz`** — Kubernetes convention

Any 2xx response = healthy. Response time > 2 s = degraded. Connection refused / non-2xx = unhealthy.

If the agent's health endpoint returns JSON with a `load_percent` or `load` field, agentns uses it for load-based routing:
```json
{ "status": "healthy", "load_percent": 42.0 }
```

---

## Deployment patterns

### Sidecar (recommended)
One agentns per orchestrator host. Agents in all regions register with it.

```
┌─────────────────────────────────────────┐
│ Orchestrator host                        │
│  ┌────────────────┐  ┌────────────────┐ │
│  │  orchestrator  │→ │  agentns :8200 │ │
│  └────────────────┘  └────────────────┘ │
└─────────────────────────────────────────┘
         ↕ register                ↕ register
  ┌──────────────┐          ┌──────────────┐
  │ agent-nyc    │          │ agent-london │
  │  :9001       │          │  :9001       │
  └──────────────┘          └──────────────┘
```

### Centralised
One shared agentns for the whole system. Use MongoDB to persist registrations.

```yaml
# docker-compose.yml
services:
  agentns:
    image: ghcr.io/DataWorksAI-com/agentns:latest
    ports: ["8200:8200"]
    environment:
      MONGODB_URI: "mongodb+srv://..."
      AGENTNS_NAMESPACE: "mycompany.production"
```

### Embedded (Python only)
Mount the FastAPI app directly into your own app:

```python
from fastapi import FastAPI
from agentns.server import app as ans_app

main_app = FastAPI()
main_app.mount("/ans", ans_app)
```

---

## Contributing

PRs welcome. Run tests with:

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

---

## License

MIT © 2025 DataWorksAI
