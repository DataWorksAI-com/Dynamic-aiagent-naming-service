"""
agentns.requester_lib
=====================
Client library for **requester agents** — agents that need to discover and
call other agents in a multi-agent system.

This is one half of the agentns client split:
  requester_lib.py → resolve other agents  (you WANT to call someone)
  target_lib.py    → register your agent   (you ARE the target)

Quick start
-----------
    import agentns

    client = agentns.requester_lib.connect()

    # One-liner resolve:
    endpoint = await client.resolve(agentns.Query.from_label("alerts"))

    # With context (preferred city, protocol):
    endpoint = await client.resolve(agentns.Query(
        agent_name        = agentns.AgentName.from_label("planner"),
        requester_context = agentns.RequesterContext(
            location  = {"city": "Boston"},
            protocols = ["A2A"],
        ),
    ))

    if endpoint:
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.post(endpoint.url, json={"message": "Plan my trip"})

Environment variables
---------------------
    AGENTNS_RESOLVER_URL — resolver URL      (default: AGENTNS_URL or http://localhost:8200)
    AGENTNS_API_KEY      — API key           (required if resolver has auth enabled)
    ANS_TLD              — URN TLD           (used by AgentName.from_label)
    ANS_APP              — URN namespace     (used by AgentName.from_label)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .urn_parser import parse_urn, ParsedURN


# ── AgentName ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentName:
    """
    Immutable, validated agent identity.

    Supports three identity formats:
    • URN:       urn:agents.example.com:my-app:alerts
    • Email-like: alerts.my-app#agents.example.com
    • DNS-like:  _alerts._my-app.agent.agents.example.com

    Constructors
    ------------
    AgentName.from_urn("urn:agents.example.com:my-app:alerts")
    AgentName.from_parts("agents.example.com", "my-app", "alerts")
    AgentName.from_label("alerts")   # reads ANS_TLD + ANS_APP from env
    """

    _raw: str

    # ── Constructors ──────────────────────────────────────────────────────────

    @classmethod
    def from_urn(cls, urn: str) -> "AgentName":
        """Create from a full URN. Raises ValueError if malformed."""
        parse_urn(urn)  # validate immediately
        return cls(_raw=urn)

    @classmethod
    def from_parts(cls, tld: str, namespace: str, label: str) -> "AgentName":
        """Create from individual URN components."""
        return cls(_raw=f"urn:{tld}:{namespace}:{label}")

    @classmethod
    def from_label(cls, label: str) -> "AgentName":
        """
        Create from a short label using ANS_TLD and ANS_APP env vars.

        Environment:
            ANS_TLD  — TLD portion of the URN  (default: "agentns.local")
            ANS_APP  — namespace portion        (default: "default")

        Example:
            ANS_TLD=agents.example.com  ANS_APP=my-app
            AgentName.from_label("alerts")
            → urn:agents.example.com:my-app:alerts
        """
        tld = os.getenv("ANS_TLD", "agentns.local")
        ns  = os.getenv("ANS_APP", "default")
        return cls.from_parts(tld, ns, label)

    # ── Parsing ───────────────────────────────────────────────────────────────

    def parse(self) -> ParsedURN:
        """Return the parsed URN components (tld, app_namespace, label)."""
        return parse_urn(self._raw)

    def to_urn(self) -> str:
        """Return the canonical URN string."""
        return self._raw

    def __str__(self) -> str:
        return self._raw

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def label(self) -> str:
        """Short agent label, e.g. "alerts"."""
        return self.parse().label

    @property
    def namespace(self) -> str:
        """Application namespace, e.g. "my-app"."""
        return self.parse().namespace

    @property
    def tld(self) -> str:
        """Top-level domain, e.g. "agents.example.com"."""
        return self.parse().tld


# ── RequesterContext ───────────────────────────────────────────────────────────

@dataclass
class RequesterContext:
    """
    Metadata about the requesting agent — used to optimise endpoint selection.

    The resolver uses this to pick the geographically closest, least loaded
    server that supports your preferred protocol.

    Fields
    ------
    location:       Where are you? Used for geo-routing.
                    {"city": "Boston"}  or  {"latitude": 42.36, "longitude": -71.06}

    protocols:      Which protocols can you speak? (default: ["A2A"])
                    The resolver picks the best matching protocol from the server's list.

    device:         Optional device type hint. e.g. "mobile", "iot", "server"
    network:        Optional network type hint. e.g. "5g", "wifi", "ethernet"
    security_level: Required security level. "low" | "medium" | "high" (default: "medium")
    """
    location:       Dict      = field(default_factory=dict)
    protocols:      List[str] = field(default_factory=lambda: ["A2A"])
    device:         str       = ""
    network:        str       = ""
    security_level: str       = "medium"

    def to_dict(self) -> Dict:
        return {
            "location":       self.location,
            "protocols":      self.protocols,
            "device":         self.device,
            "network":        self.network,
            "security_level": self.security_level,
        }


# ── Query ──────────────────────────────────────────────────────────────────────

@dataclass
class Query:
    """
    A resolution request — who you want (AgentName) + context about you (RequesterContext).

    Usage
    -----
        # Minimal (env vars provide TLD + namespace):
        q = Query.from_label("alerts")

        # With requester location:
        q = Query(
            agent_name        = AgentName.from_label("planner"),
            requester_context = RequesterContext(location={"city": "Boston"}),
        )

        # Skip cache for a fresh lookup:
        q = Query(AgentName.from_label("alerts"), cache_enabled=False)
    """
    agent_name:        AgentName
    requester_context: RequesterContext = field(default_factory=RequesterContext)
    cache_enabled:     bool             = True

    @classmethod
    def from_label(cls, label: str, **ctx_kwargs) -> "Query":
        """
        Shorthand — create a Query from a short label.

        Pass additional keyword args to set RequesterContext fields:
            Query.from_label("alerts", location={"city": "Boston"})
        """
        return cls(
            agent_name=AgentName.from_label(label),
            requester_context=RequesterContext(**ctx_kwargs) if ctx_kwargs else RequesterContext(),
        )


# ── TailoredEndpoint ──────────────────────────────────────────────────────────

@dataclass
class TailoredEndpoint:
    """
    A resolved, ready-to-use agent endpoint — the result of a successful resolve() call.

    Fields
    ------
    url:            The endpoint URL to call. This is your primary field.
                    If via_proxy=True, this is the A2A proxy URL.
                    If via_proxy=False, this is the direct agent URL.

    protocol:       The protocol to use (e.g. "A2A", "SLIM").

    ttl:            How long (in seconds) to cache this result.
                    After TTL expires, re-resolve to get a fresh endpoint.

    via_proxy:      True if url points to an A2A proxy (routed through SLIM).
                    False if url is a direct agent endpoint.

    slim_identity:  SLIM routing identity in "org/namespace/label" format.
                    e.g. "mbta/transit-ci/alerts"
                    Used when calling through SLIM directly.

    region:         Region label, e.g. "us-east" or "Boston, MA".
    cached:         True if this result came from cache.
    selected_by:    How the server was selected: "geo_nearest", "lowest_latency", etc.
    resolution_time_ms: How long the resolution took.
    metadata:       Additional data from the resolver.
    flag:           Emoji flag for the server region. e.g. "🇺🇸"

    Backward compatibility
    ----------------------
    .endpoint and .endpoint_url are aliases for .url — code using ResolvedAgent
    (the old client.py type) continues to work without changes.
    """
    url:                str
    protocol:           str
    ttl:                int
    via_proxy:          bool  = False
    slim_identity:      str   = ""
    region:             str   = ""
    cached:             bool  = False
    selected_by:        str   = ""
    resolution_time_ms: float = 0.0
    metadata:           Dict  = field(default_factory=dict)
    flag:               str   = ""

    # ── Backward-compat aliases ────────────────────────────────────────────────

    @property
    def endpoint(self) -> str:
        """Alias for url — backward compatibility with ResolvedAgent."""
        return self.url

    @property
    def endpoint_url(self) -> str:
        """Alias for url — backward compatibility."""
        return self.url


# ── RequesterAgentClient ──────────────────────────────────────────────────────

class RequesterAgentClient:
    """
    Resolve agent names to live endpoints.

    Never raises — returns None on any failure so callers can implement their
    own fallback logic (e.g. fall back to a hardcoded URL or a registry lookup).

    Usage
    -----
        client = agentns.requester_lib.connect()

        endpoint = await client.resolve(Query.from_label("alerts"))
        if endpoint is None:
            # resolution failed — use fallback
            endpoint_url = FALLBACK_URL
        else:
            endpoint_url = endpoint.url

        async with httpx.AsyncClient() as c:
            r = await c.post(endpoint_url, json={"message": "..."})
    """

    def __init__(
        self,
        resolver_url: str,
        timeout: float = 5.0,
        api_key: str = "",
    ):
        self.resolver_url = resolver_url.rstrip("/")
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        # Persistent client — reuses TCP connections across resolve() calls.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def resolve(self, query: Query) -> Optional[TailoredEndpoint]:
        """
        Resolve the agent named in ``query`` to a live endpoint.

        Returns TailoredEndpoint on success, None on any failure (network error,
        agent not found, resolver down, etc.).

        Never raises.
        """
        payload: Dict[str, Any] = {
            "agent_name":    query.agent_name.to_urn(),
            "cache_enabled": query.cache_enabled,
        }
        if query.requester_context:
            payload["requester_context"] = query.requester_context.to_dict()

        try:
            resp = await self._client.post(f"{self.resolver_url}/resolve", json=payload)
            if resp.status_code != 200:
                return None
            data = resp.json()
            return TailoredEndpoint(
                url                = data.get("url") or data.get("endpoint", ""),
                protocol           = data.get("protocol", "A2A"),
                ttl                = data.get("ttl", 60),
                via_proxy          = data.get("via_proxy", False),
                slim_identity      = data.get("slim_identity", ""),
                region             = data.get("region", ""),
                cached             = data.get("cached", False),
                selected_by        = data.get("selected_by", ""),
                resolution_time_ms = data.get("resolution_time_ms", 0.0),
                metadata           = data.get("metadata", {}),
                flag               = data.get("flag", ""),
            )
        except Exception:
            return None

    async def health(self) -> Dict:
        """Check the resolver's own health."""
        try:
            resp = await self._client.get(f"{self.resolver_url}/health")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Call on agent shutdown."""
        await self._client.aclose()


# ── connect() factory ──────────────────────────────────────────────────────────

def connect(
    resolver_url: Optional[str] = None,
    timeout: float = 5.0,
    api_key: Optional[str] = None,
) -> RequesterAgentClient:
    """
    Create a RequesterAgentClient — the entry point for requester agents.

    Parameters
    ----------
    resolver_url: Resolver URL.
                  Defaults to AGENTNS_RESOLVER_URL env var, then AGENTNS_URL, then
                  http://localhost:8200.
    timeout:      HTTP timeout in seconds (default: 5.0).
    api_key:      API key for authenticated resolvers.
                  Defaults to AGENTNS_API_KEY env var.

    Example
    -------
        # Reads from environment:
        client = agentns.requester_lib.connect()

        # Explicit:
        client = agentns.requester_lib.connect(
            resolver_url = "http://my-agentns:8200",
            api_key      = "my-secret-key",
        )
    """
    url = (
        resolver_url
        or os.getenv("AGENTNS_RESOLVER_URL")
        or os.getenv("AGENTNS_URL")
        or "http://localhost:8200"
    )
    key = api_key if api_key is not None else os.getenv("AGENTNS_API_KEY", "")
    return RequesterAgentClient(url, timeout=timeout, api_key=key)
