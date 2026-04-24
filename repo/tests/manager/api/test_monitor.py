"""
Tests for the health check monitor API endpoints.

These tests cover the in-memory-backed REST API introduced in Step 1.
No database or polling logic is involved at this stage.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from aiohttp import web


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cors_options() -> dict:
    return {
        "*": MagicMock(
            allow_credentials=False,
            expose_headers="*",
            allow_headers="*",
        )
    }


def _build_app() -> web.Application:
    """Return a fresh aiohttp Application with the monitor sub-app mounted."""
    from ai.backend.manager.api.monitor import create_app

    app = web.Application()
    subapp, _ = create_app(_make_cors_options())
    app.add_subapp("/monitor", subapp)
    return app


# ---------------------------------------------------------------------------
# GET /monitor/services
# ---------------------------------------------------------------------------


class TestListServices:
    """Tests for GET /api/monitor/services"""

    async def test_list_services_empty(self, aiohttp_client) -> None:
        """Should return empty list when no services are monitored."""
        client = await aiohttp_client(_build_app())
        resp = await client.get("/monitor/services")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"services": []}

    async def test_list_services_with_data(self, aiohttp_client) -> None:
        """Should return list of services after one has been added."""
        client = await aiohttp_client(_build_app())

        # Add a service first
        add_resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health"},
        )
        assert add_resp.status == 201

        resp = await client.get("/monitor/services")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["services"]) == 1
        svc = data["services"][0]
        assert svc["url"] == "https://example.com/health"
        assert svc["last_check_time"] is None
        assert svc["last_status_code"] is None
        assert svc["last_latency_ms"] is None
        assert svc["status"] is None
        assert "id" in svc
        assert "created_at" in svc


# ---------------------------------------------------------------------------
# POST /monitor/services
# ---------------------------------------------------------------------------


class TestAddService:
    """Tests for POST /api/monitor/services"""

    async def test_add_service_success(self, aiohttp_client) -> None:
        """Should create a new service and return 201 with the created record."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert "service" in data
        svc = data["service"]
        assert svc["url"] == "https://example.com/health"
        assert svc["status"] is None
        assert svc["last_check_time"] is None
        assert svc["last_status_code"] is None
        assert svc["last_latency_ms"] is None
        # id must be a valid UUID string
        uuid.UUID(svc["id"])

    async def test_add_service_with_name(self, aiohttp_client) -> None:
        """Should accept an optional name field."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health", "name": "My Service"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["service"]["name"] == "My Service"

    async def test_add_service_name_defaults_to_url(self, aiohttp_client) -> None:
        """When name is omitted the URL should be used as the display name."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["service"]["name"] == "https://example.com/health"

    async def test_add_service_invalid_url(self, aiohttp_client) -> None:
        """Should return 400 when URL is not a valid HTTP/HTTPS URL."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "not-a-valid-url"},
        )
        assert resp.status in (400, 422)

    async def test_add_service_missing_url(self, aiohttp_client) -> None:
        """Should return 400 when the url field is absent."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"name": "No URL here"},
        )
        assert resp.status == 400

    async def test_add_service_malformed_body(self, aiohttp_client) -> None:
        """Should return 400 when the request body is not valid JSON."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            data=b"this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# DELETE /monitor/services/{id}
# ---------------------------------------------------------------------------


class TestDeleteService:
    """Tests for DELETE /api/monitor/services/{id}"""

    async def test_delete_service_success(self, aiohttp_client) -> None:
        """Should delete a service and return 204."""
        client = await aiohttp_client(_build_app())

        # Create a service to delete
        add_resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health"},
        )
        assert add_resp.status == 201
        service_id = (await add_resp.json())["service"]["id"]

        resp = await client.delete(f"/monitor/services/{service_id}")
        assert resp.status == 204

        # Confirm it is gone
        list_resp = await client.get("/monitor/services")
        data = await list_resp.json()
        assert data == {"services": []}

    async def test_delete_service_not_found(self, aiohttp_client) -> None:
        """Should return 404 when service does not exist."""
        client = await aiohttp_client(_build_app())
        resp = await client.delete(f"/monitor/services/{uuid.uuid4()}")
        assert resp.status == 404

    async def test_delete_service_invalid_id(self, aiohttp_client) -> None:
        """Should return 400 when service ID is not a valid UUID."""
        client = await aiohttp_client(_build_app())
        resp = await client.delete("/monitor/services/not-a-uuid")
        assert resp.status == 400
