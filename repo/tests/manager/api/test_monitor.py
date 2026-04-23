"""
Tests for the health check monitor API endpoints.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web


@pytest.fixture
def mock_root_ctx():
    """Create a mock root context with a mock database."""
    ctx = MagicMock()
    ctx.db = MagicMock()
    return ctx


class TestListServices:
    """Tests for GET /api/monitor/services"""

    async def test_list_services_empty(self, aiohttp_client, mock_root_ctx):
        """Should return empty list when no services are monitored."""
        from ai.backend.manager.api.monitor import create_app

        # Mock DB to return empty result
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=MagicMock(fetchall=lambda: []))
        mock_root_ctx.db.begin_readonly = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        app = web.Application()
        app["_root.context"] = mock_root_ctx

        cors_options = {
            "*": MagicMock(
                allow_credentials=False,
                expose_headers="*",
                allow_headers="*",
            )
        }
        subapp, _ = create_app(cors_options)
        app.add_subapp("/monitor", subapp)

        client = await aiohttp_client(app)
        resp = await client.get("/monitor/services")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"services": []}

    async def test_list_services_with_data(self, aiohttp_client, mock_root_ctx):
        """Should return list of services with their status."""
        from datetime import datetime, timezone

        from ai.backend.manager.api.monitor import create_app

        service_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        mock_row = {
            "id": service_id,
            "url": "https://example.com/health",
            "last_check_time": now,
            "last_status_code": 200,
            "last_latency_ms": 42.5,
            "status": "up",
            "created_at": now,
        }

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(
            return_value=MagicMock(fetchall=lambda: [mock_row])
        )
        mock_root_ctx.db.begin_readonly = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        app = web.Application()
        app["_root.context"] = mock_root_ctx

        cors_options = {
            "*": MagicMock(
                allow_credentials=False,
                expose_headers="*",
                allow_headers="*",
            )
        }
        subapp, _ = create_app(cors_options)
        app.add_subapp("/monitor", subapp)

        client = await aiohttp_client(app)
        resp = await client.get("/monitor/services")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["services"]) == 1
        svc = data["services"][0]
        assert svc["id"] == str(service_id)
        assert svc["url"] == "https://example.com/health"
        assert svc["last_status_code"] == 200
        assert svc["last_latency_ms"] == 42.5
        assert svc["status"] == "up"


class TestAddService:
    """Tests for POST /api/monitor/services"""

    async def test_add_service_success(self, aiohttp_client, mock_root_ctx):
        """Should create a new service and return 201 with the created record."""
        from datetime import datetime, timezone

        from ai.backend.manager.api.monitor import create_app

        service_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        mock_row = {
            "id": service_id,
            "url": "https://example.com/health",
            "last_check_time": None,
            "last_status_code": None,
            "last_latency_ms": None,
            "status": None,
            "created_at": now,
        }

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(
            side_effect=[
                MagicMock(),  # insert result
                MagicMock(first=lambda: mock_row),  # select result
            ]
        )
        mock_root_ctx.db.begin = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        app = web.Application()
        app["_root.context"] = mock_root_ctx

        cors_options = {
            "*": MagicMock(
                allow_credentials=False,
                expose_headers="*",
                allow_headers="*",
            )
        }
        subapp, _ = create_app(cors_options)
        app.add_subapp("/monitor", subapp)

        client = await aiohttp_client(app)
        resp = await client.post(
            "/monitor/services",
            json={"url": "https://example.com/health"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert "service" in data
        assert data["service"]["url"] == "https://example.com/health"
        assert data["service"]["status"] is None

    async def test_add_service_invalid_url(self, aiohttp_client, mock_root_ctx):
        """Should return 400 when URL is invalid."""
        from ai.backend.manager.api.monitor import create_app

        app = web.Application()
        app["_root.context"] = mock_root_ctx

        cors_options = {
            "*": MagicMock(
                allow_credentials=False,
                expose_headers="*",
                allow_headers="*",
            )
        }
        subapp, _ = create_app(cors_options)
        app.add_subapp("/monitor", subapp)

        client = await aiohttp_client(app)
        resp = await client.post(
            "/monitor/services",
            json={"url": "not-a-valid-url"},
        )
        # trafaret URL validation should reject this
        assert resp.status in (400, 422)


class TestDeleteService:
    """Tests for DELETE /api/monitor/services/{id}"""

    async def test_delete_service_success(self, aiohttp_client, mock_root_ctx):
        """Should delete a service and return 204."""
        from ai.backend.manager.api.monitor import create_app

        service_id = uuid.uuid4()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=MagicMock(rowcount=1))
        mock_root_ctx.db.begin = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        app = web.Application()
        app["_root.context"] = mock_root_ctx

        cors_options = {
            "*": MagicMock(
                allow_credentials=False,
                expose_headers="*",
                allow_headers="*",
            )
        }
        subapp, _ = create_app(cors_options)
        app.add_subapp("/monitor", subapp)

        client = await aiohttp_client(app)
        resp = await client.delete(f"/monitor/services/{service_id}")
        assert resp.status == 204

    async def test_delete_service_not_found(self, aiohttp_client, mock_root_ctx):
        """Should return 404 when service does not exist."""
        from ai.backend.manager.api.monitor import create_app

        service_id = uuid.uuid4()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=MagicMock(rowcount=0))
        mock_root_ctx.db.begin = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        app = web.Application()
        app["_root.context"] = mock_root_ctx

        cors_options = {
            "*": MagicMock(
                allow_credentials=False,
                expose_headers="*",
                allow_headers="*",
            )
        }
        subapp, _ = create_app(cors_options)
        app.add_subapp("/monitor", subapp)

        client = await aiohttp_client(app)
        resp = await client.delete(f"/monitor/services/{service_id}")
        assert resp.status == 404

    async def test_delete_service_invalid_id(self, aiohttp_client, mock_root_ctx):
        """Should return 400 when service ID is not a valid UUID."""
        from ai.backend.manager.api.monitor import create_app

        app = web.Application()
        app["_root.context"] = mock_root_ctx

        cors_options = {
            "*": MagicMock(
                allow_credentials=False,
                expose_headers="*",
                allow_headers="*",
            )
        }
        subapp, _ = create_app(cors_options)
        app.add_subapp("/monitor", subapp)

        client = await aiohttp_client(app)
        resp = await client.delete("/monitor/services/not-a-uuid")
        assert resp.status == 400
