# agentns — Agent Name Service

> **Plug-and-play service discovery for multi-agent AI systems.**  
> Register agents. Resolve them by name. Route through any backend — HTTP registry, Consul, Kubernetes, or a static YAML file.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## What it does

agentns is a single-binary nameservice sidecar that:

- **Registers** agent endpoints by label (`alerts`, `planner`, etc.)
- **Resolves** agents by URN (`urn:agents.example.com:my-app:alerts`) to live, health-checked endpoints
- **Routes** to the best server using geo-proximity, load, and protocol preference
- **Plugs into any backend** — HTTP registry, Consul, Kubernetes, or static YAML
- **Secures** endpoints with API key auth + rate limiting + security headers

---

## Quick start

```bash
pip install agentns

# Dev (no auth key needed):
AGENTNS_AUTH=off agentns-server --port 8200

# Production (generate a key first):
# python -c "import secrets; print(secrets.token_urlsafe(32))"
AGENTNS_API_KEYS="your-key-here" agentns-server --port 8200
```

**Target agent** — register yourself so others can find you:

```python
import agentns

client = agentns.target_lib.connect()   # reads AGENTNS_URL + AGENTNS_API_KEY from env
await client.record(agentns.DeploymentSpec(
    leaf_name  = "alerts",
    a2a_url    = "http://myhost:9001",
    health_url = "http://myhost:9001/health",
    location   = {"city": "Boston"},
    protocols  = ["A2A"],
))
```

**Requester agent** — resolve other agents:

```python
import agentns

client   = agentns.requester_lib.connect()  # reads AGENTNS_RESOLVER_URL + AGENTNS_API_KEY from env
endpoint = await client.resolve(agentns.Query.from_label("alerts"))

if endpoint:
    import httpx
    async with httpx.AsyncClient() as c:
        r = await c.post(endpoint.url, json={"message": "Red Line status?"})
```

---

## Architecture

```
Requester Agent
  │  agentns.requester_lib.connect()
  │  await client.resolve(Query.from_label("alerts"))
  ▼
agentns server (:8200)
  │  health-checks all registered endpoints
  │  geo-ranks by proximity + load (pluggable GeoPolicy)
  │  returns TailoredEndpoint
  │
  │  optional: if A2A_PROXY_ENDPOINTS is set
  │  returns proxy URL + slim_identity instead of direct URL
  ▼
Target Agent  (or A2A Proxy → SLIM Controller → Agent SLIM listener)
  registered via agentns.target_lib.record()
```

---

## API reference

### POST /resolve

Resolve an agent URN to a live endpoint. Requires `X-API-Key` header.

```bash
curl -X POST http://localhost:8200/resolve \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "urn:agents.example.com:my-app:alerts",
    "requester_context": {"location": {"city": "Boston"}, "protocols": ["A2A"]},
    "cache_enabled": true
  }'
```

Response:

```json
{
  "url":           "http://host:9001",
  "endpoint":      "http://host:9001",
  "protocol":      "A2A",
  "ttl":           60,
  "region":        "us-east",
  "via_proxy":     false,
  "slim_identity": "",
  "cached":        false,
  "selected_by":   "geo_nearest",
  "resolution_time_ms": 4.2
}
```

When `A2A_PROXY_ENDPOINTS` is configured, `url` points to the proxy instead of the direct agent, and `via_proxy`/`slim_identity` are populated.

### POST /register

Register an agent endpoint. Requires `X-API-Key` header.

```bash
curl -X POST http://localhost:8200/register \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "label":     "alerts",
    "endpoint":  "http://myhost:9001",
    "location":  {"city": "Boston"},
    "protocols": ["A2A"]
  }'
```

### GET /health

Returns server health + all registered agents. **No auth required.**

### GET /agents

List all registered agents with live health status. **No auth required.**

---

## Pluggable registry backends

agentns can resolve agents from any backend:

```python
from agentns.registry_adapter import (
    HttpRegistryAdapter,    # POST /resolve to any HTTP registry (default)
    StaticRegistryAdapter,  # in-memory dict or YAML file (zero infra)
    MultiRegistryAdapter,   # fan-out: try multiple registries in order
    RegistryAdapter,        # ABC — implement your own (Consul, K8s, etcd...)
)
```

### Static YAML (zero infra — great for dev/testing)

```bash
REGISTRY_ADAPTER=static
REGISTRY_YAML=/etc/agentns/agents.yaml
```

```yaml
# agents.yaml
my-app:alerts:
  endpoint: http://localhost:9001
  protocol: A2A
  ttl: 60
my-app:planner:
  endpoint: http://localhost:9002
  protocol: A2A
  ttl: 60
```

### HTTP registry (default)

```bash
REGISTRY_URL=http://my-registry:6900
```

The registry must expose `POST /resolve` returning `{"endpoint", "protocol", "ttl"}`.

### Multi-registry (primary + fallback)

```bash
REGISTRY_ADAPTER=multi
REGISTRY_URLS=http://primary:6900,http://backup:6900
```

### Custom adapter — plug in Consul, Kubernetes, etcd, or anything

```python
from agentns.registry_adapter import RegistryAdapter

class ConsulAdapter(RegistryAdapter):
    async def resolve(self, agent_path, requester_context):
        import httpx
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

See the [registry adapter docs](agentns/registry_adapter.py) for Consul, Kubernetes, and multi-registry examples.

---

## Pluggable geo-selection

Control how servers are ranked when multiple instances are registered:

```python
from agentns.geo_policy import NearestPolicy, LeastLoadedPolicy, CompositePolicy
from agentns.server_selection import rank_servers

# Default: balance distance + RTT + load (CompositePolicy)
ranked = rank_servers(servers, health_map, ctx)

# Pure Haversine distance (CDN-style edge selection)
ranked = rank_servers(servers, health_map, ctx, geo_policy=NearestPolicy())

# Ignore geo, rank by current load only
ranked = rank_servers(servers, health_map, ctx, geo_policy=LeastLoadedPolicy())

# Tune composite weights
ranked = rank_servers(servers, health_map, ctx,
                      geo_policy=CompositePolicy(geo_weight=2.0, rtt_weight=0.0))
```

Write your own:

```python
from agentns.geo_policy import GeoPolicy

class LatencyOnlyPolicy(GeoPolicy):
    def score(self, server, health, requester_latlon):
        return health.get("response_time_ms", 9999.0)
```

---

## Security

### API key authentication

```bash
# Generate a secure key (≥32 chars required):
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Configure (comma-separated for zero-downtime key rotation):
export AGENTNS_API_KEYS="key1-abc...,key2-def..."
```

All POST endpoints require `X-API-Key: <key>`. GET endpoints (`/health`, `/agents`, `/namespaces`) are always open for monitoring.

To disable auth in local development only:

```bash
export AGENTNS_AUTH=off
```

### Rate limiting

```bash
pip install "agentns[server]"   # includes slowapi
```

Default limits: 60 req/min on `/resolve`, 60 req/min on `/register`, 5 req/min on `/cache/clear`.

### Security headers

All responses include `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`, and `Cache-Control: no-store`.

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `AGENTNS_PORT` | `8200` | HTTP port |
| `AGENTNS_NAMESPACE` | `agents.local` | Default URN namespace |
| `AGENTNS_TLD` | `agentns.local` | URN top-level domain |
| `AGENTNS_API_KEYS` | *(required if auth=on)* | Comma-separated API keys (≥32 chars) |
| `AGENTNS_AUTH` | `on` | `off` to disable auth (dev only) |
| `AGENTNS_HEALTH_INTERVAL` | `30` | Background health sweep interval (s) |
| `MONGODB_URI` | *(none)* | MongoDB URI (in-memory if absent) |
| `REGISTRY_ADAPTER` | `http` | `http` / `static` / `multi` |
| `REGISTRY_URL` | `http://localhost:6900` | HTTP registry URL |
| `REGISTRY_YAML` | `agents.yaml` | Static registry YAML file |
| `REGISTRY_URLS` | *(none)* | Comma-separated URLs for multi-adapter |
| `A2A_PROXY_ENDPOINTS` | *(none)* | Comma-separated A2A proxy base URLs |
| `SLIM_ORG` | *(none)* | SLIM org prefix for `slim_identity` |

**Client environment variables:**

| Variable | Default | Description |
|---|---|---|
| `AGENTNS_URL` | `http://localhost:8200` | Server URL |
| `AGENTNS_RESOLVER_URL` | *(AGENTNS_URL)* | Resolver URL (requester_lib) |
| `AGENTNS_API_KEY` | *(none)* | Client API key |
| `ANS_TLD` | `agentns.local` | TLD for `AgentName.from_label()` |
| `ANS_APP` | `default` | Namespace for `AgentName.from_label()` |

---

## Docker

```bash
# Development (no auth):
docker run -p 8200:8200 -e AGENTNS_AUTH=off ghcr.io/tonystark3110/agentns:latest

# Production:
docker run -p 8200:8200 \
  -e AGENTNS_API_KEYS="your-key-here" \
  ghcr.io/tonystark3110/agentns:latest
```

Or with full stack via [`docker-compose.yml`](docker-compose.yml).

---

## URN format

```
urn:{tld}:{namespace}:{label}
    │       │           └── agent label    e.g. "alerts"
    │       └────────────── app namespace  e.g. "my-app"
    └────────────────────── TLD           e.g. "agents.example.com"
```

Three formats are supported:

| Format | Example |
|--------|---------|
| URN (recommended) | `urn:agents.example.com:my-app:alerts` |
| Email-like | `alerts.my-app#agents.example.com` |
| DNS-like | `_alerts._my-app.agent.agents.example.com` |

---

## License

MIT — see [LICENSE](LICENSE).
