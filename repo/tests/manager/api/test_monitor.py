from __future__ import annotations

from http import HTTPStatus
from typing import Any

import aiohttp_cors
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from ai.backend.manager.api.monitor import (
    MonitoredService,
    create_app,
)


@pytest.fixture
def monitor_app() -> web.Application:
    """Create a standalone aiohttp app with the monitor sub-app mounted."""
    cors_options = {
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=False,
            expose_headers="*",
            allow_headers="*",
        ),
    }
    subapp, _ = create_app(cors_options)
    root = web.Application()
    root.add_subapp("/monitor", subapp)
    return root


@pytest.fixture
async def client(
    monitor_app: web.Application,
    aiohttp_client: Any,
) -> TestClient:
    return await aiohttp_client(monitor_app)


@pytest.mark.asyncio
async def test_list_services_empty(client: TestClient) -> None:
    resp = await client.get("/monitor/services")
    assert resp.status == HTTPStatus.OK
    data = await resp.json()
    assert data == []


@pytest.mark.asyncio
async def test_add_service(client: TestClient) -> None:
    payload = {"url": "https://example.com/health", "name": "Example Service"}
    resp = await client.post("/monitor/services", json=payload)
    assert resp.status == HTTPStatus.CREATED
    data = await resp.json()
    assert data["url"] == "https://example.com/health"
    assert data["name"] == "Example Service"
    assert "id" in data
    # After immediate health check, status fields should be populated
    assert data["status"] in ("up", "down", None)
    assert "last_check" in data
    assert "status_code" in data
    assert "latency_ms" in data


@pytest.mark.asyncio
async def test_add_service_missing_url(client: TestClient) -> None:
    payload = {"name": "No URL"}
    resp = await client.post("/monitor/services", json=payload)
    assert resp.status == HTTPStatus.BAD_REQUEST
    data = await resp.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_add_service_missing_name(client: TestClient) -> None:
    payload = {"url": "https://example.com"}
    resp = await client.post("/monitor/services", json=payload)
    assert resp.status == HTTPStatus.BAD_REQUEST
    data = await resp.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_add_service_invalid_json(client: TestClient) -> None:
    resp = await client.post(
        "/monitor/services",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == HTTPStatus.BAD_REQUEST


@pytest.mark.asyncio
async def test_list_services_after_add(client: TestClient) -> None:
    payload1 = {"url": "https://a.com/health", "name": "Service A"}
    payload2 = {"url": "https://b.com/health", "name": "Service B"}
    await client.post("/monitor/services", json=payload1)
    await client.post("/monitor/services", json=payload2)

    resp = await client.get("/monitor/services")
    assert resp.status == HTTPStatus.OK
    data = await resp.json()
    assert len(data) == 2
    names = {svc["name"] for svc in data}
    assert names == {"Service A", "Service B"}


@pytest.mark.asyncio
async def test_delete_service(client: TestClient) -> None:
    payload = {"url": "https://c.com/health", "name": "Service C"}
    resp = await client.post("/monitor/services", json=payload)
    created = await resp.json()
    service_id = created["id"]

    # Delete the service
    resp = await client.delete(f"/monitor/services/{service_id}")
    assert resp.status == HTTPStatus.NO_CONTENT

    # Verify it's gone
    resp = await client.get("/monitor/services")
    data = await resp.json()
    assert all(svc["id"] != service_id for svc in data)


@pytest.mark.asyncio
async def test_delete_service_not_found(client: TestClient) -> None:
    resp = await client.delete("/monitor/services/nonexistent-id")
    assert resp.status == HTTPStatus.NOT_FOUND
    data = await resp.json()
    assert "error" in data


def test_monitored_service_to_json() -> None:
    svc = MonitoredService(
        id="test-id",
        url="https://example.com",
        name="Test",
        status="up",
        last_check="2024-01-01T00:00:00Z",
        status_code=200,
        latency_ms=42.5,
    )
    result = svc.to_json()
    assert result == {
        "id": "test-id",
        "url": "https://example.com",
        "name": "Test",
        "status": "up",
        "last_check": "2024-01-01T00:00:00Z",
        "status_code": 200,
        "latency_ms": 42.5,
    }


def test_monitored_service_defaults() -> None:
    svc = MonitoredService(id="id-1", url="https://x.com", name="X")
    result = svc.to_json()
    assert result["status"] is None
    assert result["last_check"] is None
    assert result["status_code"] is None
    assert result["latency_ms"] is None


@pytest.mark.asyncio
async def test_add_service_down_check(client: TestClient) -> None:
    """Adding a service with a non-routable URL should result in status='down'."""
    payload = {"url": "http://192.0.2.1:9999/nope", "name": "Down Service"}
    resp = await client.post("/monitor/services", json=payload)
    assert resp.status == HTTPStatus.CREATED
    data = await resp.json()
    assert data["status"] == "down"
    assert data["last_check"] is not None
    assert data["latency_ms"] is not None
    assert data["status_code"] is None


@pytest.mark.asyncio
async def test_add_service_up_check(client: TestClient, aiohttp_server: Any) -> None:
    """Adding a service with a reachable URL should result in status='up'."""
    # Create a simple test server that returns 200
    inner_app = web.Application()
    inner_app.router.add_get("/health", lambda r: web.Response(text="OK"))
    server = await aiohttp_server(inner_app)
    url = f"http://localhost:{server.port}/health"

    payload = {"url": url, "name": "Up Service"}
    resp = await client.post("/monitor/services", json=payload)
    assert resp.status == HTTPStatus.CREATED
    data = await resp.json()
    assert data["status"] == "up"
    assert data["status_code"] == 200
    assert data["last_check"] is not None
    assert data["latency_ms"] is not None
    assert data["latency_ms"] >= 0
