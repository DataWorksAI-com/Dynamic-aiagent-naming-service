"""
agentns — Agent Name Service
============================
Single-binary service discovery for multi-agent systems.

Quick start — requester agent (resolve other agents)
-----------------------------------------------------
    import agentns

    client = agentns.requester_lib.connect()
    endpoint = await client.resolve(agentns.Query.from_label("alerts"))
    if endpoint:
        # POST to endpoint.url using your preferred HTTP client
        ...

Quick start — target agent (register yourself)
----------------------------------------------
    import agentns

    client = agentns.target_lib.connect()
    await client.record(agentns.DeploymentSpec(
        leaf_name = "alerts",
        a2a_url   = "http://myhost:9001",
    ))

Start the server
----------------
    # Via CLI:
    agentns-server --port 8200

    # Via Python:
    import uvicorn
    from agentns.server import app
    uvicorn.run(app, host="0.0.0.0", port=8200)

Environment variables
---------------------
    AGENTNS_PORT             Server port                   (default: 8200)
    AGENTNS_NAMESPACE        Default URN namespace         (default: "agents.local")
    AGENTNS_TLD              URN TLD                       (default: "agentns.local")
    AGENTNS_URL              Client → server URL           (default: http://localhost:8200)
    AGENTNS_RESOLVER_URL     Requester client resolver URL (defaults to AGENTNS_URL)
    ANS_TLD                  URN TLD for AgentName.from_label()
    ANS_APP                  URN namespace for AgentName.from_label()
    REGISTRY_ADAPTER         Registry backend: http|static|multi (default: http)
    REGISTRY_URL             HTTP registry URL             (default: http://localhost:6900)
    REGISTRY_YAML            Static registry YAML file     (for REGISTRY_ADAPTER=static)
    REGISTRY_URLS            Comma-separated registry URLs (for REGISTRY_ADAPTER=multi)
    MONGODB_URI              MongoDB connection string     (optional; in-memory if absent)
"""

__version__ = "3.0.0"
__author__  = "DataWorksAI"
__license__ = "MIT"

# ── Requester side ─────────────────────────────────────────────────────────────
from agentns.requester_lib import (
    connect as resolve_connect,
    AgentName,
    RequesterContext,
    Query,
    TailoredEndpoint,
    RequesterAgentClient,
)

# ── Target side ────────────────────────────────────────────────────────────────
from agentns.target_lib import (
    connect as record_connect,
    DeploymentSpec,
    TargetAgentClient,
)

# ── Sub-module aliases (agentns.requester_lib.connect() etc.) ─────────────────
from agentns import requester_lib, target_lib

__all__ = [
    # Requester side
    "resolve_connect",
    "AgentName",
    "RequesterContext",
    "Query",
    "TailoredEndpoint",
    "RequesterAgentClient",
    "requester_lib",
    # Target side
    "record_connect",
    "DeploymentSpec",
    "TargetAgentClient",
    "target_lib",
]
