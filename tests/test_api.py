"""Integration tests for the agentns FastAPI server."""
import pytest
from httpx import AsyncClient, ASGITransport
from agentns.server import app, _registry, _health_cache, _cache


@pytest.fixture(autouse=True)
async def clear_state():
    """Reset global state between tests."""
    _registry.clear()
    _health_cache.clear()
    await _cache.clear()
    yield
    _registry.clear()
    _health_cache.clear()
    await _cache.clear()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health_empty(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["total_labels"] == 0


@pytest.mark.asyncio
async def test_register_and_list(client):
    resp = await client.post("/register", json={
        "label": "emailer",
        "endpoint": "http://test-agent:9001",
        "region": "us-east",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "registered"
    assert data["label"] == "emailer"
    assert data["total_endpoints"] == 1
    assert data["agent_name"] == "urn:agentns.local:agents.local:emailer"

    resp = await client.get("/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert "emailer" in agents
    assert agents["emailer"][0]["endpoint"] == "http://test-agent:9001"


@pytest.mark.asyncio
async def test_register_update(client):
    await client.post("/register", json={"label": "emailer", "endpoint": "http://host:9001"})
    resp = await client.post("/register", json={"label": "emailer", "endpoint": "http://host:9001"})
    data = resp.json()
    assert data["status"] == "updated"
    assert data["total_endpoints"] == 1


@pytest.mark.asyncio
async def test_register_two_replicas(client):
    await client.post("/register", json={"label": "emailer", "endpoint": "http://nyc:9001"})
    resp = await client.post("/register", json={"label": "emailer", "endpoint": "http://lon:9001"})
    data = resp.json()
    assert data["total_endpoints"] == 2


@pytest.mark.asyncio
async def test_resolve_by_label(client):
    await client.post("/register", json={"label": "emailer", "endpoint": "http://test:9001"})

    # Inject healthy status so rank_servers picks it
    _health_cache["http://test:9001"] = {
        "status": "healthy", "load": 30.0, "response_time_ms": 50.0, "last_check": "now"
    }

    resp = await client.post("/resolve", json={"label": "emailer"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["endpoint"] == "http://test:9001"


@pytest.mark.asyncio
async def test_resolve_by_urn(client):
    await client.post("/register", json={"label": "emailer", "endpoint": "http://test:9001"})
    _health_cache["http://test:9001"] = {
        "status": "healthy", "load": 30.0, "response_time_ms": 50.0, "last_check": "now"
    }

    resp = await client.post("/resolve", json={
        "agent_name": "urn:agentns.local:agents.local:emailer"
    })
    assert resp.status_code == 200
    assert resp.json()["endpoint"] == "http://test:9001"


@pytest.mark.asyncio
async def test_resolve_unknown_tld_no_federation(client):
    """URN with a TLD that has no registered remote registry returns 404."""
    resp = await client.post("/resolve", json={
        "agent_name": "urn:wrong.com:agents.local:emailer"
    })
    assert resp.status_code == 404
    assert "No registry" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_resolve_different_namespace_same_tld(client):
    """URN with correct TLD but a different namespace resolves normally (no namespace check)."""
    # Register with any label; namespace in URN is not validated locally
    await client.post("/register", json={
        "label": "emailer",
        "endpoint": "http://test-other:9001"
    })
    _health_cache["http://test-other:9001"] = {
        "status": "healthy", "load": 30.0, "response_time_ms": 50.0, "last_check": "now"
    }
    resp = await client.post("/resolve", json={
        "agent_name": "urn:agentns.local:other-app:emailer"
    })
    assert resp.status_code == 200
    assert resp.json()["endpoint"] == "http://test-other:9001"


@pytest.mark.asyncio
async def test_resolve_plain_label_no_check(client):
    """Plain label (no URN) skips namespace check entirely."""
    await client.post("/register", json={"label": "emailer", "endpoint": "http://test:9001"})
    _health_cache["http://test:9001"] = {
        "status": "healthy", "load": 30.0, "response_time_ms": 50.0, "last_check": "now"
    }
    resp = await client.post("/resolve", json={"label": "emailer"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_resolve_unknown_label(client):
    resp = await client.post("/resolve", json={"label": "nonexistent"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resolve_missing_body(client):
    resp = await client.post("/resolve", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_deregister_specific(client):
    await client.post("/register", json={"label": "emailer", "endpoint": "http://nyc:9001"})
    await client.post("/register", json={"label": "emailer", "endpoint": "http://lon:9001"})

    resp = await client.request("DELETE", "/register/emailer",
                                json={"endpoint": "http://nyc:9001"})
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1
    assert "emailer" in _registry
    assert len(_registry["emailer"]) == 1


@pytest.mark.asyncio
async def test_deregister_via_query_param(client):
    """DELETE /register/label?endpoint=... (cloud proxy-safe path)."""
    await client.post("/register", json={"label": "emailer", "endpoint": "http://nyc:9001"})
    await client.post("/register", json={"label": "emailer", "endpoint": "http://lon:9001"})

    resp = await client.request(
        "DELETE", "/register/emailer",
        params={"endpoint": "http://nyc:9001"},   # query param, no body
    )
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1
    assert len(_registry["emailer"]) == 1


@pytest.mark.asyncio
async def test_deregister_all(client):
    await client.post("/register", json={"label": "emailer", "endpoint": "http://nyc:9001"})
    resp = await client.request("DELETE", "/register/emailer", json={})
    assert resp.status_code == 200
    assert "emailer" not in _registry


@pytest.mark.asyncio
async def test_cache_stats(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "hits" in data
    assert "misses" in data


@pytest.mark.asyncio
async def test_cache_clear(client):
    resp = await client.post("/cache/clear")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cleared"


@pytest.mark.asyncio
async def test_resolve_always_has_url_field(client):
    """Resolved response must always include 'url' regardless of health status."""
    await client.post("/register", json={"label": "emailer", "endpoint": "http://test:9001"})
    _health_cache["http://test:9001"] = {
        "status": "healthy", "load": 30.0, "response_time_ms": 50.0, "last_check": "now"
    }
    resp = await client.post("/resolve", json={"label": "emailer"})
    assert resp.status_code == 200
    assert "url" in resp.json()


@pytest.mark.asyncio
async def test_resolve_emergency_fallback_has_url(client):
    """Emergency fallback (all unhealthy) must still return a 'url' field."""
    await client.post("/register", json={"label": "emailer", "endpoint": "http://test:9001"})
    _health_cache["http://test:9001"] = {
        "status": "unhealthy", "load": 100.0, "response_time_ms": 0.0, "last_check": "now"
    }
    resp = await client.post("/resolve", json={"label": "emailer"})
    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data
    assert data["selected_by"] == "emergency_fallback"


@pytest.mark.asyncio
async def test_namespaces(client):
    await client.post("/register", json={
        "label": "emailer", "endpoint": "http://host:9001", "namespace": "acme.sales"
    })
    resp = await client.get("/namespaces")
    data = resp.json()
    assert "acme.sales" in data["namespaces"]
    assert "emailer" in data["namespaces"]["acme.sales"]


@pytest.mark.asyncio
async def test_proxy_unknown_label_returns_404(client):
    """Proxy to an unregistered label must return 404."""
    resp = await client.post("/proxy/nonexistent", json={"message": "hi"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_proxy_url_format(client):
    """Proxy URL /proxy/{label} and /proxy/{label}/{path} both route correctly."""
    await client.post("/register", json={"label": "emailer", "endpoint": "http://host:9001"})

    # We can't actually forward to a real agent in unit tests,
    # but we can verify the label exists and the proxy finds an endpoint.
    # A 502 (upstream refused) means the proxy resolved the label successfully.
    resp = await client.post("/proxy/emailer", json={"method": "message/send", "params": {}})
    assert resp.status_code in (200, 502, 504)  # resolved label, upstream unreachable in test

    resp2 = await client.get("/proxy/emailer/.well-known/agent.json")
    assert resp2.status_code in (200, 502)  # resolved label


@pytest.mark.asyncio
async def test_health_exposes_proxy_config(client):
    """GET /health must always include a 'proxy' key."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "proxy" in data
    assert "enabled" in data["proxy"]
