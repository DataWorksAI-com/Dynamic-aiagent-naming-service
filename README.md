# agentns — Agent Name Service

> **Service discovery and proxy for multi-agent AI systems.**  
> Agents register by name. Others find them, route to the healthiest replica, and call them through a built-in proxy.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## What it does

agentns is a single-binary sidecar that runs alongside your multi-agent system:

- **Registry** — agents register themselves by name at startup
- **Proxy** — other agents call `POST /proxy/alerts` — agentns forwards to the best healthy instance
- **Health checking** — probes every registered endpoint every 30s, removes unhealthy ones from routing
- **Geo-routing** — when multiple replicas exist, picks the geographically closest + least loaded
- **MongoDB persistence** — optional; registry survives restarts without agents re-registering

---

## Quick start

```bash
pip install agentns
agentns-server --port 8200
```

Or with Docker:

```bash
docker run -p 8200:8200 ghcr.io/tonystark3110/agentns:latest
```

**Register your agent** at startup:

```python
import agentns

client = agentns.target_lib.connect()   # reads AGENTNS_URL from env
await client.record(agentns.DeploymentSpec(
    leaf_name  = "alerts",
    a2a_url    = "http://myhost:9001",
    health_url = "http://myhost:9001/health",
    location   = {"city": "Boston"},
    protocols  = ["A2A"],
))
```

**Call another agent** through the proxy:

```python
import httpx

# agentns picks the best healthy replica and forwards your request
resp = await httpx.AsyncClient().post(
    "http://agentns:8200/proxy/alerts",
    json={"method": "message/send", "params": {"text": "hello"}}
)
```

Or resolve first and call directly:

```python
import agentns

client   = agentns.requester_lib.connect()
endpoint = await client.resolve(agentns.Query.from_label("alerts"))

if endpoint:
    resp = await httpx.AsyncClient().post(endpoint.url, json={...})
```

---

## Architecture

```
Target Agent
  │  agentns.target_lib.connect()
  │  await client.record(DeploymentSpec(...))
  ▼
agentns server (:8200)                  ← single binary
  │  stores endpoint in registry
  │  health-checks every 30s
  │  optional: persists to MongoDB
  │
  ◀── POST /proxy/alerts ──────────────── Requester Agent
  │  picks best healthy replica
  │  geo-ranks by proximity + load
  │  forwards request, streams response back
  ▼
Target Agent  (best healthy instance)
```

---

## API reference

### ANY /proxy/{label}[/{path}]

Forward a request to the best healthy endpoint registered under `label`.

```bash
# Forward a call to the "alerts" agent:
curl -X POST http://localhost:8200/proxy/alerts \
  -H "Content-Type: application/json" \
  -d '{"method": "message/send", "params": {"text": "hello"}}'

# Forward with a path:
curl http://localhost:8200/proxy/alerts/tasks/send

# Get A2A agent card (url field auto-rewritten to proxy URL):
curl http://localhost:8200/proxy/alerts/.well-known/agent.json
```

All HTTP methods are supported (GET, POST, PUT, DELETE, PATCH). Streaming responses (SSE) work transparently.

### POST /register

Register an agent endpoint.

```bash
curl -X POST http://localhost:8200/register \
  -H "Content-Type: application/json" \
  -d '{
    "label":     "alerts",
    "endpoint":  "http://myhost:9001",
    "location":  {"city": "Boston"},
    "protocols": ["A2A"]
  }'
```

Response:
```json
{
  "status":          "registered",
  "label":           "alerts",
  "endpoint":        "http://myhost:9001",
  "agent_name":      "urn:agentns.local:agents.local:alerts",
  "total_endpoints": 1,
  "geo_routing":     "active"
}
```

### POST /resolve

Resolve a label or URN to the best live endpoint URL (without proxying).

```bash
curl -X POST http://localhost:8200/resolve \
  -H "Content-Type: application/json" \
  -d '{
    "label": "alerts",
    "requester_context": {"location": {"city": "Boston"}, "protocols": ["A2A"]}
  }'
```

Response:
```json
{
  "url":                "http://host:9001",
  "protocol":           "A2A",
  "ttl":                60,
  "region":             "us-east",
  "cached":             false,
  "selected_by":        "geo_nearest",
  "resolution_time_ms": 4.2
}
```

### DELETE /register/{label}

Remove an endpoint.

```bash
# Remove a specific endpoint:
curl -X DELETE "http://localhost:8200/register/alerts?endpoint=http://myhost:9001"

# Remove all endpoints for a label:
curl -X DELETE http://localhost:8200/register/alerts
```

### GET /health

Server health + all registered agents with live status.

```bash
curl http://localhost:8200/health | jq .
```

### GET /agents

List all registered agents with health status.

### GET /namespaces

List all namespaces and which labels are in each.

### GET /cache/stats

Resolution cache hit rate and entry counts.

### POST /cache/clear

Flush the resolution cache.

---

## MongoDB persistence

By default the registry lives in memory — fast, zero setup, but lost on restart.

### Docker Compose with local MongoDB

```bash
docker compose --profile mongo up
```

This starts agentns + a local MongoDB container. The registry persists across restarts.

### MongoDB Atlas (cloud, recommended for production)

```bash
docker run -p 8200:8200 \
  -e MONGODB_URI="mongodb+srv://user:pass@cluster0.abc.mongodb.net/" \
  ghcr.io/tonystark3110/agentns:latest
```

Free tier at [mongodb.com/atlas](https://mongodb.com/atlas) is enough for most deployments.

### What MongoDB stores

Every registered endpoint is stored in a `agents` collection:

```json
{
  "label":           "alerts",
  "endpoint":        "http://10.0.1.5:9001",
  "namespace":       "my-app",
  "region":          "us-east",
  "location":        {"city": "Boston", "latitude": 42.36, "longitude": -71.06},
  "protocols":       ["A2A"],
  "agent_name":      "urn:agentns.local:my-app:alerts",
  "registered_at":   "2025-01-01T00:00:00Z",
  "last_seen":       "2025-01-01T12:00:00Z"
}
```

On startup, agentns loads all documents into memory and begins health-checking them. Agents do not need to re-register.

---

## Geo-routing

When multiple replicas of an agent are registered, agentns picks the best one by scoring:

```
score = (geo_weight × distance_km) + (rtt_weight × response_ms) + (load_weight × load%)
```

Pass your location when resolving to enable geo-routing:

```python
endpoint = await client.resolve(agentns.Query(
    agent_name        = agentns.AgentName.from_label("alerts"),
    requester_context = agentns.RequesterContext(
        location  = {"city": "Boston"},
        protocols = ["A2A"],
    ),
))
```

**Pluggable policies:**

```python
from agentns.geo_policy import NearestPolicy, LeastLoadedPolicy, CompositePolicy
from agentns.server_selection import rank_servers

ranked = rank_servers(servers, health_map, ctx)                          # default: composite
ranked = rank_servers(servers, health_map, ctx, geo_policy=NearestPolicy())     # distance only
ranked = rank_servers(servers, health_map, ctx, geo_policy=LeastLoadedPolicy()) # load only
```

160+ cities built-in. Unknown cities resolved via OpenStreetMap Nominatim (free, no API key).

---

## Pluggable registry backends

```python
from agentns.registry_adapter import (
    HttpRegistryAdapter,   # forward to any HTTP registry
    StaticRegistryAdapter, # in-memory dict or YAML file
    MultiRegistryAdapter,  # fan-out: try multiple registries in order
    RegistryAdapter,       # ABC — implement your own
)
```

### Static YAML

```bash
REGISTRY_ADAPTER=static REGISTRY_YAML=/etc/agentns/agents.yaml agentns-server
```

```yaml
# agents.yaml
my-app:alerts:
  endpoint: http://localhost:9001
  protocol: A2A
  ttl: 60
```

### Multi-registry (primary + fallback)

```bash
REGISTRY_ADAPTER=multi
REGISTRY_URLS=http://primary:6900,http://backup:6900
```

### Custom adapter

```python
from agentns.registry_adapter import RegistryAdapter

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

---

## Agentgateway integration (optional A2A proxy)

[Agentgateway](https://agentgateway.dev) adds enterprise features (OAuth, audit logs) on top of agentns's built-in proxy.

```bash
docker run -p 8200:8200 \
  -e AGENTNS_PROXY_HOST=agentgateway \
  -e AGENTNS_PROXY_PORT=8400 \
  ghcr.io/tonystark3110/agentns:latest
```

When set, `/resolve` returns the gateway URL instead of the direct agent URL. See [`examples/agentgateway/`](examples/agentgateway/).

---

## Cloud deployment

### Step 1 — Deploy agentns on one server

```bash
# On your server (EC2, GCP VM, Azure VM):
export MONGODB_URI="mongodb+srv://user:pass@cluster.mongodb.net/"

docker run -d --restart always \
  -p 8200:8200 \
  -e MONGODB_URI="$MONGODB_URI" \
  ghcr.io/tonystark3110/agentns:latest
```

Open port 8200 in your firewall.

### Step 2 — Set env vars on every agent

```bash
AGENTNS_URL=http://your-agentns-server:8200
```

### Step 3 — Register at startup

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

### Step 4 — Call other agents through the proxy

```python
import httpx

resp = await httpx.AsyncClient().post(
    f"http://{os.environ['AGENTNS_URL']}/proxy/alerts",
    json={"method": "message/send", "params": {...}}
)
```

---

## URN format

```
urn:{tld}:{namespace}:{label}
    │       │           └── agent label    e.g. "alerts"
    │       └────────────── app namespace  e.g. "my-app"
    └────────────────────── TLD           e.g. "agents.example.com"
```

| Format | Example |
|--------|---------|
| URN (recommended) | `urn:agents.example.com:my-app:alerts` |
| Email-like | `alerts.my-app#agents.example.com` |
| DNS-like | `_alerts._my-app.agent.agents.example.com` |

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `AGENTNS_PORT` | `8200` | HTTP port |
| `AGENTNS_NAMESPACE` | `agents.local` | Default URN namespace |
| `AGENTNS_TLD` | `agentns.local` | URN top-level domain |
| `AGENTNS_HEALTH_INTERVAL` | `30` | Background health sweep interval (s) |
| `AGENTNS_GEOCODING` | `on` | Set `off` to disable Nominatim geocoding |
| `MONGODB_URI` | *(none)* | MongoDB URI — in-memory if absent |
| `MONGODB_DB` | `agentns` | MongoDB database name |
| `AGENTNS_PROXY_HOST` | *(none)* | Agentgateway hostname |
| `AGENTNS_PROXY_PORT` | `8400` | Agentgateway port |
| `AGENTNS_PROXY_MODE` | `agentgateway` | `agentgateway` or `custom` |
| `A2A_PROXY_ENDPOINTS` | *(none)* | Low-level: full proxy base URL(s) |
| `SLIM_ORG` | *(none)* | SLIM org prefix for `slim_identity` |
| `REGISTRY_ADAPTER` | `http` | `http` / `static` / `multi` |
| `REGISTRY_URL` | `http://localhost:6900` | HTTP registry URL |
| `REGISTRY_YAML` | `agents.yaml` | Static registry YAML path |
| `REGISTRY_URLS` | *(none)* | Comma-separated URLs for multi-adapter |

**Client env vars:**

| Variable | Default | Description |
|---|---|---|
| `AGENTNS_URL` | `http://localhost:8200` | Server URL (target + requester) |
| `AGENTNS_RESOLVER_URL` | *(AGENTNS_URL)* | Resolver URL override |
| `ANS_TLD` | `agentns.local` | TLD for `AgentName.from_label()` |
| `ANS_APP` | `default` | Namespace for `AgentName.from_label()` |

---

## Docker

```bash
# Development:
docker run -p 8200:8200 ghcr.io/tonystark3110/agentns:latest

# With MongoDB Atlas:
docker run -p 8200:8200 \
  -e MONGODB_URI="mongodb+srv://user:pass@cluster.mongodb.net/" \
  ghcr.io/tonystark3110/agentns:latest

# Full local stack (agentns + MongoDB):
docker compose --profile mongo up
```

---

## Security headers

All responses include:

```
X-Content-Type-Options: nosniff
X-Frame-Options:        DENY
X-XSS-Protection:       1; mode=block
Referrer-Policy:        no-referrer
Cache-Control:          no-store
```

---

## License

MIT — see [LICENSE](LICENSE).
