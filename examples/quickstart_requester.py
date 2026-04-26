"""
quickstart_requester.py
=======================
Minimal example: resolve an agent and call it.

Prerequisites:
    pip install agentns httpx
    agentns-server --port 8200   (in another terminal)

Environment (optional — defaults work for local dev):
    AGENTNS_URL=http://localhost:8200
    AGENTNS_API_KEY=your-key          # required if server has auth enabled
    ANS_TLD=agentns.local             # or your custom TLD
    ANS_APP=default                   # or your namespace
"""

import asyncio
import agentns


async def main():
    # Connect to the resolver (reads AGENTNS_URL + AGENTNS_API_KEY from env)
    client = agentns.requester_lib.connect()

    # Resolve by label (reads ANS_TLD + ANS_APP from env to build the URN)
    endpoint = await client.resolve(agentns.Query.from_label("my-agent"))

    if endpoint is None:
        print("Could not resolve 'my-agent' — is it registered?")
        return

    print(f"Resolved to: {endpoint.url}")
    print(f"  Protocol:   {endpoint.protocol}")
    print(f"  TTL:        {endpoint.ttl}s")
    print(f"  Via proxy:  {endpoint.via_proxy}")
    print(f"  Region:     {endpoint.region or 'unknown'}")
    if endpoint.slim_identity:
        print(f"  SLIM ID:    {endpoint.slim_identity}")

    # Now call the agent via A2A
    import httpx
    async with httpx.AsyncClient() as c:
        resp = await c.post(
            endpoint.url,
            json={
                "jsonrpc": "2.0",
                "method":  "message/send",
                "id":      "1",
                "params": {
                    "message": {
                        "messageId": "quickstart-1",
                        "role":      "user",
                        "parts":     [{"kind": "text", "text": "Hello, agent!"}],
                    }
                },
            },
            timeout=30.0,
        )
        print(f"\nAgent response ({resp.status_code}):")
        print(resp.text[:500])


asyncio.run(main())
