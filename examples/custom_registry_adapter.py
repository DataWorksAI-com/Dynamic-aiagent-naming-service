"""
custom_registry_adapter.py
===========================
Examples of how to plug in a custom backend registry
so agentns can discover agents from Consul, Kubernetes,
etcd, or any other service registry.

Usage:
    python custom_registry_adapter.py
"""

import asyncio
import os
from agentns.registry_adapter import (
    RegistryAdapter,
    StaticRegistryAdapter,
    MultiRegistryAdapter,
    HttpRegistryAdapter,
)


# ── Example 1: Static YAML (zero infrastructure) ──────────────────────────────
# Perfect for development, testing, and simple deployments.
# No external services needed.

async def example_static():
    print("=== StaticRegistryAdapter ===")

    adapter = StaticRegistryAdapter({
        "my-app:alerts": {
            "endpoint": "http://localhost:9001",
            "protocol": "A2A",
            "ttl":      60,
            "region":   "local",
        },
        "my-app:planner": {
            "endpoint": "http://localhost:9002",
            "protocol": "A2A",
            "ttl":      60,
        },
    })

    result = await adapter.resolve("my-app:alerts", {"protocols": ["A2A"]})
    print(f"  alerts  → {result}")

    result = await adapter.resolve("my-app:unknown", {})
    print(f"  unknown → {result}")   # None

    health = await adapter.health()
    print(f"  health  → {health}")


# ── Example 2: Consul adapter (custom implementation) ──────────────────────────
# Wire agentns to Consul service discovery.

class ConsulAdapter(RegistryAdapter):
    """
    Resolve agents from Consul service registry.

    Consul must be running and agents registered as Consul services.
    Service name = agent label (e.g. "alerts" → consul service "alerts").

    Environment:
        CONSUL_URL — Consul HTTP API URL (default: http://localhost:8500)
    """

    def __init__(self, consul_url: str = None):
        self.consul_url = (consul_url or os.getenv("CONSUL_URL", "http://localhost:8500")).rstrip("/")

    async def resolve(self, agent_path: str, requester_context: dict):
        import httpx
        label = agent_path.split(":")[-1]
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(
                    f"{self.consul_url}/v1/health/service/{label}",
                    params={"passing": "true"},
                )
            if r.status_code != 200:
                return None
            services = r.json()
            if not services:
                return None
            svc = services[0]["Service"]
            return {
                "endpoint": f"http://{svc['Address']}:{svc['Port']}",
                "protocol": "A2A",
                "ttl":      30,
                "metadata": {"consul_service_id": svc.get("ID", "")},
            }
        except Exception as exc:
            print(f"ConsulAdapter error: {exc}")
            return None

    async def health(self):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{self.consul_url}/v1/status/leader")
            return {"status": "ok", "consul_url": self.consul_url} if r.status_code == 200 else {
                "status": "error", "consul_url": self.consul_url
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}


# ── Example 3: Kubernetes adapter ─────────────────────────────────────────────
# Resolve agents from Kubernetes services using in-cluster DNS.

class KubernetesAdapter(RegistryAdapter):
    """
    Resolve agents from Kubernetes service DNS.

    In-cluster: {label}.{namespace}.svc.cluster.local:{port}
    Requires that each agent is deployed as a Kubernetes Service with the
    same name as the agent label.

    Environment:
        K8S_NAMESPACE  — Kubernetes namespace (default: "default")
        K8S_PORT       — Agent HTTP port     (default: 8080)
    """

    def __init__(self):
        self.namespace = os.getenv("K8S_NAMESPACE", "default")
        self.port      = int(os.getenv("K8S_PORT", "8080"))

    async def resolve(self, agent_path: str, requester_context: dict):
        label = agent_path.split(":")[-1]
        # Kubernetes in-cluster DNS: {service}.{namespace}.svc.cluster.local
        endpoint = f"http://{label}.{self.namespace}.svc.cluster.local:{self.port}"
        return {
            "endpoint": endpoint,
            "protocol": "A2A",
            "ttl":      300,    # DNS TTL is typically 5min in k8s
            "metadata": {
                "namespace": self.namespace,
                "k8s_dns":   f"{label}.{self.namespace}.svc.cluster.local",
            },
        }

    async def health(self):
        return {"status": "ok", "type": "kubernetes", "namespace": self.namespace}


# ── Example 4: Multi-registry with fallback ───────────────────────────────────
# Try a primary HTTP registry first, fall back to static config.

async def example_multi():
    print("\n=== MultiRegistryAdapter (primary + static fallback) ===")

    # Static fallback for when the primary registry is unavailable
    fallback = StaticRegistryAdapter({
        "prod:alerts":  {"endpoint": "http://alerts-fallback:9001", "protocol": "A2A", "ttl": 10},
    })

    # Primary HTTP registry (will fail if not running — falls through to fallback)
    primary = HttpRegistryAdapter("http://primary-registry:6900")

    adapter = MultiRegistryAdapter([primary, fallback])

    result = await adapter.resolve("prod:alerts", {})
    print(f"  prod:alerts → {result}")

    health = await adapter.health()
    print(f"  health      → {health['status']} ({len(health['adapters'])} adapters)")


async def main():
    await example_static()
    await example_multi()

    print("\n=== ConsulAdapter (requires Consul running) ===")
    consul = ConsulAdapter()
    health = await consul.health()
    print(f"  consul health → {health['status']}")

    print("\n=== KubernetesAdapter ===")
    k8s = KubernetesAdapter()
    result = await k8s.resolve("my-app:alerts", {})
    print(f"  my-app:alerts → {result['endpoint']}")


asyncio.run(main())
