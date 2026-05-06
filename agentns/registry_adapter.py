"""
agentns.registry_adapter
=========================
Pluggable registry backend for the agentns resolver.

The RegistryAdapter ABC decouples the resolver from any specific registry
implementation. Swap in any backend — HTTP, Consul, Kubernetes, etcd, or a
static YAML file — without touching the resolver logic.

Built-in adapters
-----------------
    HttpRegistryAdapter   — POST {url}/resolve to any HTTP registry (default)
    StaticRegistryAdapter — in-memory dict or YAML file, zero infrastructure
    MultiRegistryAdapter  — queries multiple registries, returns first hit

Custom adapter
--------------
Implement two methods and you're done:

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
                return {
                    "endpoint": f"http://{svc['Address']}:{svc['Port']}",
                    "protocol": "A2A",
                    "ttl":      30,
                }
        async def health(self):
            return {"status": "ok"}

    # Use the adapter directly (e.g. in your own resolver service or scripts):
    adapter = ConsulAdapter()
    result  = await adapter.resolve("my-app:alerts", {"protocols": ["A2A"]})

Note: adapters are standalone components. The built-in agentns server uses
its own in-memory registry (agents POST to /register directly). Adapters are
useful when you want to build a *custom resolver* that looks up agents in an
existing external registry (Consul, etcd, k8s) rather than having agents
self-register with agentns.

Registry HTTP Contract
----------------------
If you build a custom HTTP registry and want it to work with HttpRegistryAdapter,
expose one endpoint:

    POST /resolve
    Request:  {"agent_path": "my-app:alerts", "requester_context": {...}}
    Response: {"endpoint": "http://host:port", "protocol": "A2A", "ttl": 300}
    404 if agent not found, 503 if registry unhealthy.

That's the entire contract. Any language, any framework.

Selecting an adapter at runtime
--------------------------------
    REGISTRY_ADAPTER=http    (default) → HttpRegistryAdapter(REGISTRY_URL)
    REGISTRY_ADAPTER=static            → StaticRegistryAdapter(REGISTRY_YAML)
    REGISTRY_ADAPTER=multi             → MultiRegistryAdapter(REGISTRY_URLS)
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("agentns.registry")


# ── Abstract base ──────────────────────────────────────────────────────────────

class RegistryAdapter(ABC):
    """
    Abstract interface between the agentns resolver and any backend registry.

    Contract
    --------
    • resolve() must return a dict with at least:
          {"endpoint": str, "protocol": str, "ttl": int}
      or None if the agent is not found.
    • resolve() must NEVER raise — catch all exceptions, log them, return None.
    • health() returns {"status": "ok"|"error", ...} for monitoring endpoints.
    """

    @abstractmethod
    async def resolve(
        self,
        agent_path: str,
        requester_context: Dict,
    ) -> Optional[Dict]:
        """
        Resolve an agent path to an endpoint.

        Parameters
        ----------
        agent_path:        "{namespace}:{label}"  e.g. "my-app:alerts"
        requester_context: dict with location, protocols, etc.

        Returns
        -------
        dict with at least {"endpoint": str, "protocol": str, "ttl": int}
        or None if not found. Never raises.
        """
        ...

    @abstractmethod
    async def health(self) -> Dict:
        """Return {"status": "ok"|"error", ...} — used by GET /health."""
        ...


# ── HTTP adapter (default) ─────────────────────────────────────────────────────

class HttpRegistryAdapter(RegistryAdapter):
    """
    Forward resolution requests to any HTTP registry that speaks the agentns protocol.

    The remote registry must expose:
        POST /resolve
        Body:    {"agent_path": "ns:label", "requester_context": {...}}
        Returns: {"endpoint": str, "protocol": str, "ttl": int, "metadata": {...}}

    Compatible with the built-in agentns server, any MBTA-style registry,
    or any custom HTTP service that implements the two-field contract above.

    Configuration
    -------------
    REGISTRY_URL      — registry base URL        (default: http://localhost:6900)
    REGISTRY_API_KEY  — API key if registry requires X-API-Key auth
    REGISTRY_TIMEOUT  — HTTP timeout in seconds  (default: 10.0)
    """

    def __init__(
        self,
        registry_url: Optional[str] = None,
        timeout: Optional[float] = None,
        api_key: str = "",
    ):
        self.registry_url = (
            registry_url or os.getenv("REGISTRY_URL", "http://localhost:6900")
        ).rstrip("/")
        self.timeout  = timeout if timeout is not None else float(os.getenv("REGISTRY_TIMEOUT", "10.0"))
        self.api_key  = api_key or os.getenv("REGISTRY_API_KEY", "")

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    async def resolve(self, agent_path: str, requester_context: Dict) -> Optional[Dict]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
                resp = await client.post(
                    f"{self.registry_url}/resolve",
                    json={"agent_path": agent_path, "requester_context": requester_context},
                )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(
                    f"HttpRegistryAdapter: HTTP {resp.status_code} from {self.registry_url}"
                )
                return None
            return resp.json()
        except Exception as exc:
            logger.error(f"HttpRegistryAdapter.resolve({agent_path!r}) failed: {exc}")
            return None

    async def health(self) -> Dict:
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=self._headers()) as client:
                resp = await client.get(f"{self.registry_url}/health")
            if resp.status_code == 200:
                return {"status": "ok", "registry_url": self.registry_url}
            return {
                "status": "error",
                "registry_url": self.registry_url,
                "http_status": resp.status_code,
            }
        except Exception as exc:
            return {"status": "error", "registry_url": self.registry_url, "error": str(exc)}


# ── Static adapter ─────────────────────────────────────────────────────────────

class StaticRegistryAdapter(RegistryAdapter):
    """
    In-memory registry from a dict or YAML file — no external service needed.

    Great for:
    • Development and testing with zero infrastructure
    • Air-gapped / offline deployments
    • Simple single-host setups where all agents are local
    • Fallback layer in a MultiRegistryAdapter

    YAML format
    -----------
        # agents.yaml
        my-app:alerts:
          endpoint: http://localhost:9001
          protocol: A2A
          ttl: 60

        my-app:planner:
          endpoint: http://localhost:9002
          protocol: A2A
          ttl: 60

    Usage
    -----
        # From dict:
        adapter = StaticRegistryAdapter({
            "my-app:alerts": {"endpoint": "http://localhost:9001", "protocol": "A2A", "ttl": 60}
        })

        # From YAML file:
        adapter = StaticRegistryAdapter.from_yaml("agents.yaml")

        # From env var REGISTRY_YAML:
        adapter = StaticRegistryAdapter.from_env()
    """

    def __init__(self, agents: Dict[str, Dict]):
        """
        Parameters
        ----------
        agents: dict mapping "namespace:label" → {"endpoint": str, "protocol": str, "ttl": int}
                Keys are case-insensitive.
        """
        self._agents: Dict[str, Dict] = {
            k.lower(): v for k, v in (agents or {}).items()
        }

    @classmethod
    def from_yaml(cls, path: str | Path) -> "StaticRegistryAdapter":
        """Load agents from a YAML file. Requires PyYAML (pip install pyyaml)."""
        try:
            import yaml
        except ImportError:
            raise ImportError(
                "PyYAML is required for StaticRegistryAdapter.from_yaml(). "
                "Install it with: pip install pyyaml"
            )
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(data)

    @classmethod
    def from_env(cls) -> "StaticRegistryAdapter":
        """Load from the YAML path in the REGISTRY_YAML env var (default: agents.yaml)."""
        path = os.getenv("REGISTRY_YAML", "agents.yaml")
        return cls.from_yaml(path)

    def register(
        self,
        agent_path: str,
        endpoint: str,
        protocol: str = "A2A",
        ttl: int = 60,
        **extra,
    ) -> None:
        """Dynamically add or update an agent entry at runtime."""
        self._agents[agent_path.lower()] = {
            "endpoint": endpoint,
            "protocol": protocol,
            "ttl": ttl,
            **extra,
        }

    async def resolve(self, agent_path: str, requester_context: Dict) -> Optional[Dict]:
        entry = self._agents.get(agent_path.lower())
        if not entry:
            return None
        return {
            "endpoint": entry["endpoint"],
            "protocol": entry.get("protocol", "A2A"),
            "ttl":      entry.get("ttl", 60),
            "metadata": {
                k: v for k, v in entry.items()
                if k not in ("endpoint", "protocol", "ttl")
            },
        }

    async def health(self) -> Dict:
        return {
            "status": "ok",
            "type":   "static",
            "agents": len(self._agents),
        }


# ── Multi-registry adapter ─────────────────────────────────────────────────────

class MultiRegistryAdapter(RegistryAdapter):
    """
    Fan-out across multiple registries — returns the first successful result.

    Queries adapters in order; moves to the next if the current one returns None.
    Useful for:
    • Migrating between registries (try new one first, fall back to old)
    • Federating across namespaces (org registry + team registry)
    • High availability (primary + backup)
    • Static fallback when all live registries are down

    Usage
    -----
        adapter = MultiRegistryAdapter([
            HttpRegistryAdapter("http://registry-primary.example.com"),
            HttpRegistryAdapter("http://registry-backup.example.com"),
            StaticRegistryAdapter({"my-app:alerts": {...}}),  # last resort
        ])
    """

    def __init__(self, adapters: List[RegistryAdapter]):
        if not adapters:
            raise ValueError("MultiRegistryAdapter requires at least one adapter")
        self._adapters = adapters

    async def resolve(self, agent_path: str, requester_context: Dict) -> Optional[Dict]:
        for adapter in self._adapters:
            try:
                result = await adapter.resolve(agent_path, requester_context)
                if result is not None:
                    return result
            except Exception as exc:
                logger.warning(
                    f"MultiRegistryAdapter: {type(adapter).__name__} raised {exc} — trying next"
                )
        return None

    async def health(self) -> Dict:
        statuses = []
        for adapter in self._adapters:
            try:
                h = await adapter.health()
                statuses.append({"adapter": type(adapter).__name__, **h})
            except Exception as exc:
                statuses.append({
                    "adapter": type(adapter).__name__,
                    "status":  "error",
                    "error":   str(exc),
                })
        overall = "ok" if all(s.get("status") == "ok" for s in statuses) else "degraded"
        return {"status": overall, "adapters": statuses}


# ── Factory ────────────────────────────────────────────────────────────────────

def build_adapter_from_env() -> RegistryAdapter:
    """
    Build a RegistryAdapter from environment variables.

    REGISTRY_ADAPTER selects the mode:
        http     (default) — HttpRegistryAdapter using REGISTRY_URL
        static             — StaticRegistryAdapter from REGISTRY_YAML file
        multi              — MultiRegistryAdapter from REGISTRY_URLS (comma-separated)

    Examples
    --------
        # HTTP (default):
        REGISTRY_URL=http://my-registry.example.com

        # Static YAML (zero infra):
        REGISTRY_ADAPTER=static
        REGISTRY_YAML=/etc/agentns/agents.yaml

        # Multi (primary + fallback):
        REGISTRY_ADAPTER=multi
        REGISTRY_URLS=http://registry1.example.com,http://registry2.example.com
    """
    mode = os.getenv("REGISTRY_ADAPTER", "http").lower()

    if mode == "static":
        yaml_path = os.getenv("REGISTRY_YAML", "agents.yaml")
        logger.info(f"Registry adapter: static ({yaml_path})")
        return StaticRegistryAdapter.from_yaml(yaml_path)

    if mode == "multi":
        raw = os.getenv("REGISTRY_URLS", "")
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if not urls:
            raise RuntimeError(
                "REGISTRY_ADAPTER=multi but REGISTRY_URLS is empty. "
                "Set REGISTRY_URLS to a comma-separated list of registry URLs."
            )
        logger.info(f"Registry adapter: multi ({len(urls)} registries)")
        return MultiRegistryAdapter([HttpRegistryAdapter(u) for u in urls])

    # Default: http
    url = os.getenv("REGISTRY_URL", "http://localhost:6900")
    logger.info(f"Registry adapter: http ({url})")
    return HttpRegistryAdapter(url)
