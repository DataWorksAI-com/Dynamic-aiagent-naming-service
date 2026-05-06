"""
MBTA Transit — real-world agentns example
==========================================
This is the actual production use case that agentns was built from.

The MBTA multi-agent system runs three specialist agents:
  - alerts     → real-time service alerts (port 8001)
  - planner    → trip planning (port 8002)
  - stopfinder → stop lookup (port 8003)

Each agent can have multiple geographic replicas (e.g. Boston + Frankfurt).
agentns selects the best replica based on:
  1. Health (unhealthy endpoints are skipped)
  2. Geographic proximity to the requester
  3. Response latency (breaks ties)
  4. Load (CPU %)

Run this example:
    # Start agentns (dev mode — no auth key needed)
    AGENTNS_AUTH=off agentns-server --port 8200

    # Run the demo
    python examples/mbta_transit_example.py
"""

import asyncio
import os
import agentns

AGENTNS_URL = os.getenv("AGENTNS_URL", "http://localhost:8200")

# URNs follow the pattern:  urn:{tld}:{namespace}:{label}
NS  = "mbta-transit-ci"
TLD = "agents.dataworksai.com"


async def setup_mbta_agents(reg_client: agentns.TargetAgentClient):
    """Register all MBTA agents (Boston primary + Frankfurt replica)."""
    print("Registering MBTA agents...\n")

    agents = [
        # alerts — Boston primary
        agentns.DeploymentSpec(
            leaf_name  = "alerts",
            a2a_url    = "http://96.126.111.107:8001",
            health_url = "http://96.126.111.107:8001/.well-known/agent.json",
            region     = "us-east",
            location   = {"city": "Boston"},
            protocols  = ["A2A", "SLIM"],
            flag       = "🇺🇸",
        ),
        # planner — Boston primary
        agentns.DeploymentSpec(
            leaf_name  = "planner",
            a2a_url    = "http://96.126.111.107:8002",
            health_url = "http://96.126.111.107:8002/.well-known/agent.json",
            region     = "us-east",
            location   = {"city": "Boston"},
            protocols  = ["A2A", "SLIM"],
            flag       = "🇺🇸",
        ),
        # stopfinder — Boston primary
        agentns.DeploymentSpec(
            leaf_name  = "stopfinder",
            a2a_url    = "http://96.126.111.107:8003",
            health_url = "http://96.126.111.107:8003/.well-known/agent.json",
            region     = "us-east",
            location   = {"city": "Boston"},
            protocols  = ["A2A", "SLIM"],
            flag       = "🇺🇸",
        ),
        # fares — Boston primary
        agentns.DeploymentSpec(
            leaf_name = "fares",
            a2a_url   = "http://192.168.1.50:8004",
            region    = "us-east",
            location  = {"city": "Boston"},
            protocols = ["A2A"],
            flag      = "🇺🇸",
        ),
        # fares — Frankfurt replica (auto-failover if Boston is down)
        agentns.DeploymentSpec(
            leaf_name = "fares",
            a2a_url   = "http://lin-de-fra1.example.com:8004",
            region    = "eu-central",
            location  = {"city": "Frankfurt"},
            protocols = ["A2A"],
            flag      = "🇩🇪",
        ),
    ]

    for spec in agents:
        result = await reg_client.record(spec)
        print(f"  {result['status']:10s} {spec.leaf_name:12s} @ {spec.a2a_url}")

    print()


async def resolve_for_user(
    res_client: agentns.RequesterAgentClient,
    agent_label: str,
    user_city: str,
):
    """Simulate an end-user request from user_city."""
    query = agentns.Query(
        agent_name        = agentns.AgentName.from_parts(TLD, NS, agent_label),
        requester_context = agentns.RequesterContext(
            protocols = ["A2A"],
            location  = {"city": user_city},
        ),
    )
    resolved = await res_client.resolve(query)

    if resolved:
        print(
            f"  {agent_label:12s} | user in {user_city:12s} → "
            f"{resolved.flag} {resolved.region:18s} | "
            f"{resolved.metadata.get('latency_ms', '?'):>5}ms | "
            f"{resolved.selected_by}"
        )
    else:
        print(f"  {agent_label:12s} | user in {user_city:12s} → RESOLUTION FAILED")


async def main():
    # One client for registration (target side)
    reg_client = agentns.record_connect(ns_url=AGENTNS_URL)

    # One client for resolution (requester side)
    res_client = agentns.resolve_connect(resolver_url=AGENTNS_URL)

    # Check sidecar is up
    h = await res_client.health()
    print(f"agentns {h.get('status', 'unknown')} — "
          f"{h.get('total_endpoints', 0)} endpoint(s) loaded\n")

    await setup_mbta_agents(reg_client)

    # Simulate resolutions from different cities
    print("Resolving agents for different user locations:")
    print(f"  {'Agent':12s} | {'User location':24s} | {'Latency':>5s} | Selected by")
    print("  " + "─" * 70)

    test_cases = [
        ("alerts",     "Boston"),
        ("planner",    "New York"),
        ("stopfinder", "Chicago"),
        ("fares",      "Boston"),      # should pick Boston fares
        ("fares",      "Frankfurt"),   # should pick Frankfurt fares
        ("fares",      "London"),      # geographically closer to Frankfurt
    ]
    for agent_label, city in test_cases:
        await resolve_for_user(res_client, agent_label, city)

    print()

    # Cache stats after all resolutions
    stats = await res_client.health()
    print(f"agentns health: {stats.get('status')} | "
          f"{stats.get('total_labels', 0)} labels | "
          f"{stats.get('total_endpoints', 0)} endpoints")


asyncio.run(main())
