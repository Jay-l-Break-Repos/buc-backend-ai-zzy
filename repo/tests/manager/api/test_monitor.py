"""
Tests for the health check monitor API endpoints — Step 1.

Covers:
- GET /monitor/services  — list all services (empty and populated)
- POST /monitor/services — add a service (success, validation errors)
- DELETE /monitor/services/{id} — remove a service (success, not found, bad id)
- InMemoryServiceStore unit tests (add, get, list, remove, update_health)
- MonitoredService unit tests (create, to_dict)

Note: Heavy transitive dependencies (ai.backend.logging, graphene, zmq, etc.)
are stubbed out at the top of this module so the tests can run without the
full Backend.AI environment installed.
"""
from __future__ import annotations

import logging
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from aiohttp import web

# ---------------------------------------------------------------------------
# Stub out heavy transitive dependencies so the monitor modules can be
# imported in a lightweight test environment.
# ---------------------------------------------------------------------------

import types as _types

# --- ai.backend.logging stub ------------------------------------------------
if "ai.backend.logging" not in sys.modules:
    _logging_stub = _types.ModuleType("ai.backend.logging")
    _real_log = logging.getLogger("test.monitor")
    _logging_stub.BraceStyleAdapter = lambda _: MagicMock(wraps=_real_log)
    sys.modules["ai.backend.logging"] = _logging_stub

# --- ai.backend.manager.models stub -----------------------------------------
# We only stub the *package* __init__ so that the heavy graphene imports are
# skipped.  The actual monitor.py sub-module is still importable because
# Python will find it on the filesystem once the package entry is a real
# ModuleType (not a MagicMock).
if "ai.backend.manager.models" not in sys.modules:
    _models_stub = _types.ModuleType("ai.backend.manager.models")
    _models_stub.__path__ = [  # mark it as a package
        str(
            __import__("pathlib").Path(__file__).parents[3]
            / "src/ai/backend/manager/models"
        )
    ]
    _models_stub.__package__ = "ai.backend.manager.models"
    sys.modules["ai.backend.manager.models"] = _models_stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cors_options() -> dict:
    import aiohttp_cors

    return {
        "*": aiohttp_cors.ResourceOptions(
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
        """Should return all registered services."""
        client = await aiohttp_client(_build_app())

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
        assert "id" in svc
        assert "created_at" in svc

    async def test_list_services_multiple_ordered_by_creation(self, aiohttp_client) -> None:
        """Services should be returned oldest-first."""
        client = await aiohttp_client(_build_app())
        urls = [
            "https://alpha.example.com/",
            "https://beta.example.com/",
            "https://gamma.example.com/",
        ]
        for url in urls:
            r = await client.post("/monitor/services", json={"url": url})
            assert r.status == 201

        resp = await client.get("/monitor/services")
        data = await resp.json()
        returned_urls = [s["url"] for s in data["services"]]
        assert returned_urls == urls

    async def test_list_services_response_fields(self, aiohttp_client) -> None:
        """Each service record should contain all expected fields."""
        client = await aiohttp_client(_build_app())
        await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health", "name": "Example"},
        )
        resp = await client.get("/monitor/services")
        data = await resp.json()
        svc = data["services"][0]
        expected_fields = {
            "id", "url", "name", "last_check_time",
            "last_status_code", "last_latency_ms", "status", "created_at",
        }
        assert expected_fields == set(svc.keys())

    async def test_list_services_initial_status_is_none(self, aiohttp_client) -> None:
        """Before any health check, status fields should be None."""
        client = await aiohttp_client(_build_app())
        await client.post("/monitor/services", json={"url": "https://example.com/"})
        resp = await client.get("/monitor/services")
        data = await resp.json()
        svc = data["services"][0]
        assert svc["status"] is None
        assert svc["last_check_time"] is None
        assert svc["last_status_code"] is None
        assert svc["last_latency_ms"] is None


# ---------------------------------------------------------------------------
# POST /monitor/services
# ---------------------------------------------------------------------------


class TestAddService:
    """Tests for POST /api/monitor/services"""

    async def test_add_service_success(self, aiohttp_client) -> None:
        """Should create a service and return 201 with the service record."""
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
        # id must be a valid UUID string
        uuid.UUID(svc["id"])

    async def test_add_service_with_name(self, aiohttp_client) -> None:
        """Should store the provided name."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health", "name": "My Service"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["service"]["name"] == "My Service"

    async def test_add_service_name_defaults_to_url(self, aiohttp_client) -> None:
        """When name is omitted, the URL should be used as the name."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["service"]["name"] == "https://example.com/health"

    async def test_add_service_invalid_url(self, aiohttp_client) -> None:
        """Should reject non-URL strings."""
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
        """Should return 400 for non-JSON bodies sent as application/json."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            data=b"this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_add_service_response_contains_all_fields(self, aiohttp_client) -> None:
        """The created service record should contain all expected fields."""
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health", "name": "Test"},
        )
        assert resp.status == 201
        svc = (await resp.json())["service"]
        expected_fields = {
            "id", "url", "name", "last_check_time",
            "last_status_code", "last_latency_ms", "status", "created_at",
        }
        assert expected_fields == set(svc.keys())

    async def test_add_service_appears_in_list(self, aiohttp_client) -> None:
        """A newly added service should appear in the GET list."""
        client = await aiohttp_client(_build_app())
        post_resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health", "name": "Test"},
        )
        service_id = (await post_resp.json())["service"]["id"]

        list_resp = await client.get("/monitor/services")
        ids = [s["id"] for s in (await list_resp.json())["services"]]
        assert service_id in ids


# ---------------------------------------------------------------------------
# DELETE /monitor/services/{id}
# ---------------------------------------------------------------------------


class TestDeleteService:
    """Tests for DELETE /api/monitor/services/{id}"""

    async def test_delete_service_success(self, aiohttp_client) -> None:
        """Should remove the service and return 204."""
        client = await aiohttp_client(_build_app())

        add_resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health"},
        )
        assert add_resp.status == 201
        service_id = (await add_resp.json())["service"]["id"]

        resp = await client.delete(f"/monitor/services/{service_id}")
        assert resp.status == 204

        list_resp = await client.get("/monitor/services")
        data = await list_resp.json()
        assert data == {"services": []}

    async def test_delete_service_not_found(self, aiohttp_client) -> None:
        """Should return 404 for an unknown UUID."""
        client = await aiohttp_client(_build_app())
        resp = await client.delete(f"/monitor/services/{uuid.uuid4()}")
        assert resp.status == 404

    async def test_delete_service_invalid_id(self, aiohttp_client) -> None:
        """Should return 400 for a malformed (non-UUID) ID."""
        client = await aiohttp_client(_build_app())
        resp = await client.delete("/monitor/services/not-a-uuid")
        assert resp.status == 400

    async def test_delete_only_removes_target(self, aiohttp_client) -> None:
        """Deleting one service should leave others intact."""
        client = await aiohttp_client(_build_app())

        r1 = await client.post("/monitor/services", json={"url": "https://alpha.example.com/"})
        await client.post("/monitor/services", json={"url": "https://beta.example.com/"})
        id1 = (await r1.json())["service"]["id"]

        await client.delete(f"/monitor/services/{id1}")

        list_resp = await client.get("/monitor/services")
        services = (await list_resp.json())["services"]
        assert len(services) == 1
        assert services[0]["url"] == "https://beta.example.com/"


# ---------------------------------------------------------------------------
# InMemoryServiceStore unit tests
# ---------------------------------------------------------------------------


class TestInMemoryServiceStore:
    """Unit tests for the InMemoryServiceStore."""

    def _make_store(self):
        from ai.backend.manager.models.monitor import InMemoryServiceStore
        return InMemoryServiceStore()

    def _make_service(self, url: str = "https://example.com/"):
        from ai.backend.manager.models.monitor import MonitoredService
        return MonitoredService.create(url=url)

    def test_add_and_get(self) -> None:
        store = self._make_store()
        svc = self._make_service()
        store.add(svc)
        assert store.get(svc.id) is svc

    def test_get_unknown_returns_none(self) -> None:
        store = self._make_store()
        assert store.get(uuid.uuid4()) is None

    def test_list_empty(self) -> None:
        store = self._make_store()
        assert store.list() == []

    def test_list_ordered_by_creation(self) -> None:
        store = self._make_store()
        urls = ["https://a.example.com/", "https://b.example.com/", "https://c.example.com/"]
        for url in urls:
            store.add(self._make_service(url))
        listed = [s.url for s in store.list()]
        assert listed == urls

    def test_remove_existing(self) -> None:
        store = self._make_store()
        svc = self._make_service()
        store.add(svc)
        result = store.remove(svc.id)
        assert result is True
        assert store.get(svc.id) is None

    def test_remove_nonexistent(self) -> None:
        store = self._make_store()
        result = store.remove(uuid.uuid4())
        assert result is False

    def test_len(self) -> None:
        store = self._make_store()
        assert len(store) == 0
        store.add(self._make_service("https://a.example.com/"))
        store.add(self._make_service("https://b.example.com/"))
        assert len(store) == 2

    def test_update_health_2xx_marks_up(self) -> None:
        store = self._make_store()
        svc = self._make_service()
        store.add(svc)
        now = datetime.now(tz=timezone.utc)
        result = store.update_health(svc.id, status_code=200, latency_ms=42.5, checked_at=now)
        assert result is True
        updated = store.get(svc.id)
        assert updated.status == "up"
        assert updated.last_status_code == 200
        assert updated.last_latency_ms == 42.5
        assert updated.last_check_time == now

    def test_update_health_201_marks_up(self) -> None:
        store = self._make_store()
        svc = self._make_service()
        store.add(svc)
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=201, latency_ms=10.0, checked_at=now)
        assert store.get(svc.id).status == "up"

    def test_update_health_4xx_marks_down(self) -> None:
        store = self._make_store()
        svc = self._make_service()
        store.add(svc)
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=404, latency_ms=55.0, checked_at=now)
        updated = store.get(svc.id)
        assert updated.status == "down"
        assert updated.last_status_code == 404

    def test_update_health_5xx_marks_down(self) -> None:
        store = self._make_store()
        svc = self._make_service()
        store.add(svc)
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=503, latency_ms=100.0, checked_at=now)
        assert store.get(svc.id).status == "down"

    def test_update_health_none_status_code_marks_down(self) -> None:
        """A None status_code means the request failed entirely."""
        store = self._make_store()
        svc = self._make_service()
        store.add(svc)
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=None, latency_ms=9999.0, checked_at=now)
        updated = store.get(svc.id)
        assert updated.status == "down"
        assert updated.last_status_code is None

    def test_update_health_unknown_id_returns_false(self) -> None:
        store = self._make_store()
        now = datetime.now(tz=timezone.utc)
        result = store.update_health(uuid.uuid4(), status_code=200, latency_ms=1.0, checked_at=now)
        assert result is False

    def test_update_health_latency_rounded(self) -> None:
        store = self._make_store()
        svc = self._make_service()
        store.add(svc)
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=200, latency_ms=12.3456789, checked_at=now)
        assert store.get(svc.id).last_latency_ms == round(12.3456789, 3)


# ---------------------------------------------------------------------------
# MonitoredService unit tests
# ---------------------------------------------------------------------------


class TestMonitoredService:
    """Unit tests for the MonitoredService model."""

    def test_create_sets_defaults(self) -> None:
        from ai.backend.manager.models.monitor import MonitoredService

        svc = MonitoredService.create(url="https://example.com/")
        assert svc.url == "https://example.com/"
        assert svc.name == "https://example.com/"  # defaults to URL
        assert svc.status is None
        assert svc.last_check_time is None
        assert svc.last_status_code is None
        assert svc.last_latency_ms is None
        assert isinstance(svc.id, uuid.UUID)
        assert isinstance(svc.created_at, datetime)

    def test_create_with_name(self) -> None:
        from ai.backend.manager.models.monitor import MonitoredService

        svc = MonitoredService.create(url="https://example.com/", name="My Service")
        assert svc.name == "My Service"

    def test_to_dict_keys(self) -> None:
        from ai.backend.manager.models.monitor import MonitoredService

        svc = MonitoredService.create(url="https://example.com/")
        d = svc.to_dict()
        expected_keys = {
            "id", "url", "name", "last_check_time",
            "last_status_code", "last_latency_ms", "status", "created_at",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_id_is_string(self) -> None:
        from ai.backend.manager.models.monitor import MonitoredService

        svc = MonitoredService.create(url="https://example.com/")
        d = svc.to_dict()
        assert isinstance(d["id"], str)
        uuid.UUID(d["id"])  # must be a valid UUID string

    def test_to_dict_created_at_is_iso_string(self) -> None:
        from ai.backend.manager.models.monitor import MonitoredService

        svc = MonitoredService.create(url="https://example.com/")
        d = svc.to_dict()
        assert isinstance(d["created_at"], str)
        # Should be parseable as ISO 8601
        datetime.fromisoformat(d["created_at"])

    def test_unique_ids(self) -> None:
        from ai.backend.manager.models.monitor import MonitoredService

        ids = {MonitoredService.create(url="https://example.com/").id for _ in range(10)}
        assert len(ids) == 10
