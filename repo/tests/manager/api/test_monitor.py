"""
Tests for the health check monitor API endpoints — Step 2.

Covers:
- All Step 1 CRUD behaviour (list, add, delete)
- Background polling: InMemoryServiceStore.update_health()
- Immediate first-check on POST (mocked)
- Poll loop: fan-out, cancellation
- _check_service_sync: success and failure paths

Note: Heavy transitive dependencies (ai.backend.logging, graphene, zmq, etc.)
are stubbed out at the top of this module so the tests can run without the
full Backend.AI environment installed.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

# ---------------------------------------------------------------------------
# Stub out heavy transitive dependencies so the monitor modules can be
# imported in a lightweight test environment.
#
# Strategy:
#   - ai.backend.logging  → MagicMock with a real BraceStyleAdapter shim
#   - ai.backend.manager.models  → a real ModuleType so that the sub-module
#     ai.backend.manager.models.monitor can still be imported normally
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
    """Tests for GET /monitor/services"""

    async def test_list_services_empty(self, aiohttp_client) -> None:
        client = await aiohttp_client(_build_app())
        resp = await client.get("/monitor/services")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"services": []}

    async def test_list_services_with_data(self, aiohttp_client) -> None:
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


# ---------------------------------------------------------------------------
# POST /monitor/services
# ---------------------------------------------------------------------------


class TestAddService:
    """Tests for POST /monitor/services"""

    async def test_add_service_success(self, aiohttp_client) -> None:
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
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health", "name": "My Service"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["service"]["name"] == "My Service"

    async def test_add_service_name_defaults_to_url(self, aiohttp_client) -> None:
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["service"]["name"] == "https://example.com/health"

    async def test_add_service_invalid_url(self, aiohttp_client) -> None:
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"url": "not-a-valid-url"},
        )
        assert resp.status in (400, 422)

    async def test_add_service_missing_url(self, aiohttp_client) -> None:
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            json={"name": "No URL here"},
        )
        assert resp.status == 400

    async def test_add_service_malformed_body(self, aiohttp_client) -> None:
        client = await aiohttp_client(_build_app())
        resp = await client.post(
            "/monitor/services",
            data=b"this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_add_service_triggers_immediate_check(self, aiohttp_client) -> None:
        """
        POST should schedule an immediate background health check.
        We verify this by patching _check_service_async and confirming it is
        called with the newly created service.
        """
        import ai.backend.manager.api.monitor as monitor_mod

        called_with: list = []

        async def fake_check(app, service):
            called_with.append(service.url)

        with patch.object(monitor_mod, "_check_service_async", side_effect=fake_check):
            client = await aiohttp_client(_build_app())
            resp = await client.post(
                "/monitor/services",
                json={"url": "https://example.com/health"},
            )
            assert resp.status == 201
            # Give the event loop a tick to run the scheduled coroutine.
            await asyncio.sleep(0.05)

        assert "https://example.com/health" in called_with


# ---------------------------------------------------------------------------
# DELETE /monitor/services/{id}
# ---------------------------------------------------------------------------


class TestDeleteService:
    """Tests for DELETE /monitor/services/{id}"""

    async def test_delete_service_success(self, aiohttp_client) -> None:
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
        client = await aiohttp_client(_build_app())
        resp = await client.delete(f"/monitor/services/{uuid.uuid4()}")
        assert resp.status == 404

    async def test_delete_service_invalid_id(self, aiohttp_client) -> None:
        client = await aiohttp_client(_build_app())
        resp = await client.delete("/monitor/services/not-a-uuid")
        assert resp.status == 400


# ---------------------------------------------------------------------------
# InMemoryServiceStore.update_health
# ---------------------------------------------------------------------------


class TestUpdateHealth:
    """Unit tests for InMemoryServiceStore.update_health()"""

    def _make_store_with_service(self, url: str = "https://example.com/"):
        from ai.backend.manager.models.monitor import InMemoryServiceStore, MonitoredService

        store = InMemoryServiceStore()
        svc = MonitoredService.create(url=url)
        store.add(svc)
        return store, svc

    def test_update_health_2xx_marks_up(self) -> None:
        store, svc = self._make_store_with_service()
        now = datetime.now(tz=timezone.utc)
        result = store.update_health(
            svc.id, status_code=200, latency_ms=42.5, checked_at=now
        )
        assert result is True
        updated = store.get(svc.id)
        assert updated.status == "up"
        assert updated.last_status_code == 200
        assert updated.last_latency_ms == 42.5
        assert updated.last_check_time == now

    def test_update_health_201_marks_up(self) -> None:
        store, svc = self._make_store_with_service()
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=201, latency_ms=10.0, checked_at=now)
        assert store.get(svc.id).status == "up"

    def test_update_health_4xx_marks_down(self) -> None:
        store, svc = self._make_store_with_service()
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=404, latency_ms=55.0, checked_at=now)
        updated = store.get(svc.id)
        assert updated.status == "down"
        assert updated.last_status_code == 404

    def test_update_health_5xx_marks_down(self) -> None:
        store, svc = self._make_store_with_service()
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=503, latency_ms=100.0, checked_at=now)
        assert store.get(svc.id).status == "down"

    def test_update_health_none_status_code_marks_down(self) -> None:
        """A None status_code means the request failed entirely."""
        store, svc = self._make_store_with_service()
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=None, latency_ms=9999.0, checked_at=now)
        updated = store.get(svc.id)
        assert updated.status == "down"
        assert updated.last_status_code is None

    def test_update_health_unknown_id_returns_false(self) -> None:
        from ai.backend.manager.models.monitor import InMemoryServiceStore

        store = InMemoryServiceStore()
        now = datetime.now(tz=timezone.utc)
        result = store.update_health(
            uuid.uuid4(), status_code=200, latency_ms=1.0, checked_at=now
        )
        assert result is False

    def test_update_health_latency_rounded(self) -> None:
        store, svc = self._make_store_with_service()
        now = datetime.now(tz=timezone.utc)
        store.update_health(svc.id, status_code=200, latency_ms=12.3456789, checked_at=now)
        assert store.get(svc.id).last_latency_ms == round(12.3456789, 3)


# ---------------------------------------------------------------------------
# _check_service_sync (unit tests — no network)
# ---------------------------------------------------------------------------


class TestCheckServiceSync:
    """Unit tests for the synchronous HTTP check helper."""

    def test_success_returns_status_and_latency(self) -> None:
        from ai.backend.manager.api.monitor import _check_service_sync

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("requests.get", return_value=mock_resp):
            status_code, latency_ms = _check_service_sync("https://example.com/")

        assert status_code == 200
        assert latency_ms >= 0.0

    def test_connection_error_returns_none_status(self) -> None:
        from ai.backend.manager.api.monitor import _check_service_sync

        import requests as req_lib

        with patch("requests.get", side_effect=req_lib.ConnectionError("refused")):
            status_code, latency_ms = _check_service_sync("https://unreachable.example.com/")

        assert status_code is None
        assert latency_ms >= 0.0

    def test_timeout_returns_none_status(self) -> None:
        from ai.backend.manager.api.monitor import _check_service_sync

        import requests as req_lib

        with patch("requests.get", side_effect=req_lib.Timeout("timed out")):
            status_code, latency_ms = _check_service_sync("https://slow.example.com/")

        assert status_code is None

    def test_non_2xx_returns_status_code(self) -> None:
        from ai.backend.manager.api.monitor import _check_service_sync

        mock_resp = MagicMock()
        mock_resp.status_code = 503

        with patch("requests.get", return_value=mock_resp):
            status_code, latency_ms = _check_service_sync("https://example.com/")

        assert status_code == 503


# ---------------------------------------------------------------------------
# _check_service_async (integration-style, no real network)
# ---------------------------------------------------------------------------


class TestCheckServiceAsync:
    """Tests for the async wrapper that calls _check_service_sync in a thread."""

    async def test_check_updates_store_on_success(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from ai.backend.manager.api.monitor import _check_service_async
        from ai.backend.manager.models.monitor import InMemoryServiceStore, MonitoredService

        store = InMemoryServiceStore()
        svc = MonitoredService.create(url="https://example.com/")
        store.add(svc)

        app = web.Application()
        app["monitor.store"] = store
        app["monitor.executor"] = ThreadPoolExecutor(max_workers=1)

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("requests.get", return_value=mock_resp):
            await _check_service_async(app, svc)

        updated = store.get(svc.id)
        assert updated.status == "up"
        assert updated.last_status_code == 200
        assert updated.last_latency_ms is not None
        assert updated.last_check_time is not None

        app["monitor.executor"].shutdown(wait=False)

    async def test_check_updates_store_on_failure(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        import requests as req_lib

        from ai.backend.manager.api.monitor import _check_service_async
        from ai.backend.manager.models.monitor import InMemoryServiceStore, MonitoredService

        store = InMemoryServiceStore()
        svc = MonitoredService.create(url="https://down.example.com/")
        store.add(svc)

        app = web.Application()
        app["monitor.store"] = store
        app["monitor.executor"] = ThreadPoolExecutor(max_workers=1)

        with patch("requests.get", side_effect=req_lib.ConnectionError("refused")):
            await _check_service_async(app, svc)

        updated = store.get(svc.id)
        assert updated.status == "down"
        assert updated.last_status_code is None

        app["monitor.executor"].shutdown(wait=False)


# ---------------------------------------------------------------------------
# Poll loop lifecycle
# ---------------------------------------------------------------------------


class TestPollLoop:
    """Tests for the background _poll_loop task."""

    async def test_poll_loop_cancels_cleanly(self) -> None:
        """The poll loop task should handle CancelledError without raising."""
        from ai.backend.manager.api.monitor import _poll_loop
        from ai.backend.manager.models.monitor import InMemoryServiceStore

        app = web.Application()
        app["monitor.store"] = InMemoryServiceStore()

        task = asyncio.ensure_future(_poll_loop(app))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected
        assert task.done()

    async def test_poll_loop_checks_all_services(self) -> None:
        """
        After one full poll cycle every registered service should have been
        checked.  We set POLL_INTERVAL_SECONDS=0 so the loop fires immediately.
        """
        import ai.backend.manager.api.monitor as monitor_mod
        from ai.backend.manager.models.monitor import InMemoryServiceStore, MonitoredService

        store = InMemoryServiceStore()
        urls = [
            "https://alpha.example.com/",
            "https://beta.example.com/",
        ]
        for url in urls:
            store.add(MonitoredService.create(url=url))

        checked: list[str] = []

        async def fake_check(app, service):
            checked.append(service.url)

        app = web.Application()
        app["monitor.store"] = store

        original_interval = monitor_mod.POLL_INTERVAL_SECONDS
        monitor_mod.POLL_INTERVAL_SECONDS = 0  # fire immediately

        try:
            with patch.object(monitor_mod, "_check_service_async", side_effect=fake_check):
                task = asyncio.ensure_future(monitor_mod._poll_loop(app))
                # Allow the loop to complete one cycle
                await asyncio.sleep(0.1)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            monitor_mod.POLL_INTERVAL_SECONDS = original_interval

        assert set(checked) == set(urls)
