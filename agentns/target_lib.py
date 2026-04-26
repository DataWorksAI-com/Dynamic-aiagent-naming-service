"""
agentns.target_lib
==================
Client library for **target agents** — agents that register themselves
so they can be discovered by other agents.

This is one half of the agentns client split:
  target_lib.py   → register/deregister your agent  (you ARE the target)
  requester_lib.py→ resolve other agents             (you WANT to call someone)

Quick start
-----------
    import agentns

    # At agent startup — register so others can find you:
    client = agentns.target_lib.connect()
    await client.record(agentns.DeploymentSpec(
        leaf_name  = "alerts",
        a2a_url    = "http://myhost:9001",
        health_url = "http://myhost:9001/health",
        region     = "us-east",
        location   = {"city": "Boston"},
        protocols  = ["A2A"],
    ))

    # At agent shutdown — remove yourself:
    await client.deregister("alerts", "http://myhost:9001")

Environment variables
---------------------
    AGENTNS_URL      — nameservice URL  (default: http://localhost:8200)
    AGENTNS_API_KEY  — API key          (required if nameservice has auth enabled)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx


# ── DeploymentSpec ─────────────────────────────────────────────────────────────

@dataclass
class DeploymentSpec:
    """
    Describes how a target agent is deployed and how to reach it.

    Pass to TargetAgentClient.record() at agent startup.

    Fields
    ------
    leaf_name:  Short label for this agent. Must be unique within your namespace.
                e.g. "alerts", "planner", "my-custom-agent"

    a2a_url:    The A2A-compatible HTTP endpoint for this agent.
                e.g. "http://myhost:9001"
                This is the URL other agents will call after resolution.

    health_url: Optional health check URL. Probed to verify liveness.
                e.g. "http://myhost:9001/health"
                If empty, the nameservice auto-discovers via /.well-known/agent.json

    region:     Optional region identifier for geo-routing.
                e.g. "us-east", "eu-west", "ap-southeast"

    location:   Optional geographic location for proximity-based selection.
                Accepts city name or explicit coordinates:
                  {"city": "Boston"}
                  {"latitude": 42.36, "longitude": -71.06}

    protocols:  List of supported protocols. Default: ["A2A"]
                Add "SLIM" if your agent also runs a SLIM transport listener.

    flag:       Optional emoji flag for display purposes. e.g. "🇺🇸"
    """
    leaf_name:  str
    a2a_url:    str
    health_url: str       = ""
    region:     str       = ""
    location:   Dict      = field(default_factory=dict)
    protocols:  List[str] = field(default_factory=lambda: ["A2A"])
    flag:       str       = ""


# ── TargetAgentClient ──────────────────────────────────────────────────────────

class TargetAgentClient:
    """
    Register and manage a target agent's presence in the agentns nameservice.

    All methods are async. Use ``connect()`` to create an instance.

    Usage
    -----
        client = agentns.target_lib.connect()

        # Register (idempotent — safe to call on every startup):
        result = await client.record(DeploymentSpec(
            leaf_name = "my-agent",
            a2a_url   = "http://myhost:9000",
        ))

        # Deregister on shutdown:
        await client.deregister("my-agent", "http://myhost:9000")
    """

    def __init__(
        self,
        ns_url: str,
        timeout: float = 5.0,
        api_key: str = "",
    ):
        self.ns_url  = ns_url.rstrip("/")
        self.timeout = timeout
        self._headers: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            self._headers["X-API-Key"] = api_key

    # ── record (register) ──────────────────────────────────────────────────────

    async def record(self, spec: DeploymentSpec) -> Dict:
        """
        Register this agent's endpoint with the nameservice.

        Idempotent — if the same endpoint is already registered, it updates it.
        Call this at agent startup and after any config change.

        Returns the server's registration response dict.
        Raises httpx.HTTPStatusError on failure.
        """
        payload = {
            "label":             spec.leaf_name,
            "endpoint":          spec.a2a_url,
            "health_check_url":  spec.health_url,
            "region":            spec.region,
            "location":          spec.location,
            "protocols":         spec.protocols,
            "flag":              spec.flag,
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers) as client:
            resp = await client.post(f"{self.ns_url}/register", json=payload)
            resp.raise_for_status()
            return resp.json()

    # ── deregister ────────────────────────────────────────────────────────────

    async def deregister(self, leaf_name: str, a2a_url: str = "") -> Dict:
        """
        Remove this agent from the nameservice.

        Call this on graceful shutdown so traffic is not routed to a dead endpoint.

        Parameters
        ----------
        leaf_name: Agent label (e.g. "alerts")
        a2a_url:   Specific endpoint to remove. If empty, removes ALL endpoints for label.
        """
        body = {"endpoint": a2a_url} if a2a_url else {}
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers) as client:
            resp = await client.request(
                "DELETE", f"{self.ns_url}/register/{leaf_name}", json=body
            )
            resp.raise_for_status()
            return resp.json()

    # ── health ────────────────────────────────────────────────────────────────

    async def health(self) -> Dict:
        """Check the nameservice's own health. Does not require authentication."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.ns_url}/health")
            resp.raise_for_status()
            return resp.json()


# ── connect() factory ──────────────────────────────────────────────────────────

def connect(
    ns_url: Optional[str] = None,
    timeout: float = 5.0,
    api_key: Optional[str] = None,
) -> TargetAgentClient:
    """
    Create a TargetAgentClient — the entry point for target agents.

    Parameters
    ----------
    ns_url:  Nameservice URL.
             Defaults to AGENTNS_URL env var, then http://localhost:8200.
    timeout: HTTP timeout in seconds (default: 5.0).
    api_key: API key for authenticated nameservice.
             Defaults to AGENTNS_API_KEY env var.

    Example
    -------
        # Reads AGENTNS_URL and AGENTNS_API_KEY from environment:
        client = agentns.target_lib.connect()

        # Explicit:
        client = agentns.target_lib.connect(
            ns_url  = "http://my-agentns:8200",
            api_key = "my-secret-key",
        )
    """
    url = ns_url or os.getenv("AGENTNS_URL", "http://localhost:8200")
    key = api_key if api_key is not None else os.getenv("AGENTNS_API_KEY", "")
    return TargetAgentClient(url, timeout=timeout, api_key=key)
