"""
agentns.client
==============
Lightweight async (and sync) client for the agentns sidecar.

Usage — async
-------------
    from agentns.client import AgentNSClient

    client = AgentNSClient("http://localhost:8200")

    # Resolve
    result = await client.resolve("urn:myco.com:sales:emailer")
    print(result.endpoint)   # → "http://host:8080"

    # Register (call from agent startup)
    await client.register("emailer", "http://host:8080", region="us-east")

Usage — sync (wraps asyncio.run)
---------------------------------
    from agentns.client import AgentNSClientSync

    client = AgentNSClientSync("http://localhost:8200")
    result = client.resolve("urn:myco.com:sales:emailer")

Environment variable shortcut
------------------------------
    AGENTNS_URL=http://localhost:8200

    client = AgentNSClient()   # reads from env
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_URL = os.getenv("AGENTNS_URL", "http://localhost:8200")


@dataclass
class ResolvedAgent:
    endpoint:   str
    protocol:   str
    ttl:        int
    region:     str
    cached:     bool
    selected_by: str
    resolution_time_ms: float
    metadata:   Dict = field(default_factory=dict)
    flag:       str  = ""

    @property
    def endpoint_url(self) -> str:
        return self.endpoint


class AgentNSClient:
    """Async agentns client."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        timeout: float = 5.0,
    ) -> None:
        self.url     = url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    # ── resolve ────────────────────────────────────────────────────────────────

    async def resolve(
        self,
        agent_name: str,
        *,
        requester_context: Optional[Dict] = None,
        cache_enabled: bool = True,
    ) -> Optional[ResolvedAgent]:
        """
        Resolve *agent_name* (URN or label) → ResolvedAgent.
        Returns None on failure (never raises) — caller should fall back.
        """
        payload: Dict[str, Any] = {
            "agent_name":      agent_name,
            "cache_enabled":   cache_enabled,
        }
        if requester_context:
            payload["requester_context"] = requester_context

        try:
            resp = await self._client.post("/resolve", json=payload)
            if resp.status_code != 200:
                return None
            data = resp.json()
            return ResolvedAgent(
                endpoint           = data["endpoint"],
                protocol           = data.get("protocol", "http"),
                ttl                = data.get("ttl", 60),
                region             = data.get("region", ""),
                cached             = data.get("cached", False),
                selected_by        = data.get("selected_by", ""),
                resolution_time_ms = data.get("resolution_time_ms", 0.0),
                metadata           = data.get("metadata", {}),
                flag               = data.get("flag", ""),
            )
        except Exception:
            return None

    # ── register ───────────────────────────────────────────────────────────────

    async def register(
        self,
        label: str,
        endpoint: str,
        *,
        namespace: Optional[str] = None,
        region: str = "",
        region_label: str = "",
        location: Optional[Dict] = None,
        protocols: Optional[List[str]] = None,
        health_check_url: str = "",
        flag: str = "",
    ) -> Dict:
        """Register an agent endpoint. Returns the server response dict."""
        payload: Dict[str, Any] = {
            "label":    label,
            "endpoint": endpoint,
            "region":   region,
        }
        if namespace:       payload["namespace"]        = namespace
        if region_label:    payload["region_label"]     = region_label
        if location:        payload["location"]         = location
        if protocols:       payload["protocols"]        = protocols
        if health_check_url: payload["health_check_url"] = health_check_url
        if flag:            payload["flag"]             = flag

        resp = await self._client.post("/register", json=payload)
        resp.raise_for_status()
        return resp.json()

    # ── deregister ─────────────────────────────────────────────────────────────

    async def deregister(self, label: str, endpoint: str = "") -> Dict:
        body = {"endpoint": endpoint} if endpoint else {}
        resp = await self._client.request("DELETE", f"/register/{label}", json=body)
        resp.raise_for_status()
        return resp.json()

    # ── health ────────────────────────────────────────────────────────────────

    async def health(self) -> Dict:
        resp = await self._client.get("/health")
        resp.raise_for_status()
        return resp.json()

    # ── context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "AgentNSClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        await self._client.aclose()


class AgentNSClientSync:
    """
    Synchronous wrapper around AgentNSClient.
    One call → asyncio.run() per method.  Fine for scripts and startup code.
    For high-throughput use the async client.
    """

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 5.0) -> None:
        self._url     = url
        self._timeout = timeout

    def _run(self, coro):
        return asyncio.run(coro)

    def resolve(self, agent_name: str, **kwargs) -> Optional[ResolvedAgent]:
        async def _go():
            async with AgentNSClient(self._url, self._timeout) as c:
                return await c.resolve(agent_name, **kwargs)
        return self._run(_go())

    def register(self, label: str, endpoint: str, **kwargs) -> Dict:
        async def _go():
            async with AgentNSClient(self._url, self._timeout) as c:
                return await c.register(label, endpoint, **kwargs)
        return self._run(_go())

    def health(self) -> Dict:
        async def _go():
            async with AgentNSClient(self._url, self._timeout) as c:
                return await c.health()
        return self._run(_go())
