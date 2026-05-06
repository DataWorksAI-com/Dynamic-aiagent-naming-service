"""
quickstart_target.py
====================
Minimal example: register your agent with agentns at startup.

Call this from your agent's startup code so other agents can discover it.

Prerequisites:
    pip install agentns
    AGENTNS_AUTH=off agentns-server --port 8200   (in another terminal)

Environment (optional — defaults work for local dev):
    AGENTNS_URL=http://localhost:8200
    AGENTNS_API_KEY=your-key          # required if server has auth enabled
"""

import asyncio
import atexit
import agentns

MY_LABEL    = "my-agent"
MY_ENDPOINT = "http://localhost:9000"


async def register():
    client = agentns.target_lib.connect()

    result = await client.record(agentns.DeploymentSpec(
        leaf_name  = MY_LABEL,
        a2a_url    = MY_ENDPOINT,
        health_url = f"{MY_ENDPOINT}/health",
        region     = "us-east",
        location   = {"city": "New York"},
        protocols  = ["A2A"],
    ))

    print(f"Registered: {result}")
    return client


async def deregister(client: agentns.TargetAgentClient):
    result = await client.deregister(MY_LABEL, MY_ENDPOINT)
    print(f"Deregistered: {result}")


async def main():
    client = await register()

    print(f"\n'{MY_LABEL}' is now discoverable at {MY_ENDPOINT}")
    print("Other agents can find it with:")
    print(f"  endpoint = await resolver_client.resolve(Query.from_label('{MY_LABEL}'))")
    print("\nPress Ctrl+C to deregister and exit.\n")

    try:
        await asyncio.sleep(3600)   # keep alive; replace with your real agent loop
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await deregister(client)


asyncio.run(main())
