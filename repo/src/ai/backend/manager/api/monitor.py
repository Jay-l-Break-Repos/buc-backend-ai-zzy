from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from http import HTTPStatus
from typing import Any, Tuple

import aiohttp_cors
import attrs
from aiohttp import web

from ai.backend.logging import BraceStyleAdapter

from .types import CORSOptions, WebMiddleware

log = BraceStyleAdapter(logging.getLogger(__spec__.name))


@attrs.define(slots=True)
class MonitoredService:
    """Data model for a monitored service endpoint."""

    id: str
    url: str
    name: str
    is_up: bool | None = None
    last_check_time: str | None = None
    http_status_code: int | None = None
    latency_ms: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "name": self.name,
            "is_up": self.is_up,
            "last_check_time": self.last_check_time,
            "http_status_code": self.http_status_code,
            "latency_ms": self.latency_ms,
        }


# In-memory storage for monitored services is stored per-app in app["monitor.services"].


def _get_services_store(app: web.Application) -> dict[str, MonitoredService]:
    """Retrieve the in-memory services store from the app context."""
    return app["monitor.services"]


async def list_services(request: web.Request) -> web.Response:
    """GET /api/monitor/services — List all monitored services."""
    log.info("MONITOR.LIST_SERVICES ()")
    store = _get_services_store(request.app)
    services = [svc.to_json() for svc in store.values()]
    return web.json_response(services)


async def add_service(request: web.Request) -> web.Response:
    """POST /api/monitor/services — Add a new URL to monitor."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "Invalid JSON body"},
            status=HTTPStatus.BAD_REQUEST,
        )

    url = body.get("url")
    name = body.get("name")

    if not url or not isinstance(url, str):
        return web.json_response(
            {"error": "Field 'url' is required and must be a non-empty string"},
            status=HTTPStatus.BAD_REQUEST,
        )
    if not name or not isinstance(name, str):
        return web.json_response(
            {"error": "Field 'name' is required and must be a non-empty string"},
            status=HTTPStatus.BAD_REQUEST,
        )

    service_id = str(uuid.uuid4())
    service = MonitoredService(
        id=service_id,
        url=url,
        name=name,
    )

    store = _get_services_store(request.app)
    store[service_id] = service

    log.info("MONITOR.ADD_SERVICE (id:{}, url:{}, name:{})", service_id, url, name)
    return web.json_response(service.to_json(), status=HTTPStatus.CREATED)


async def delete_service(request: web.Request) -> web.Response:
    """DELETE /api/monitor/services/:id — Remove a monitored endpoint."""
    service_id = request.match_info["service_id"]
    store = _get_services_store(request.app)

    if service_id not in store:
        log.warning("MONITOR.DELETE_SERVICE — not found (id:{})", service_id)
        return web.json_response(
            {"error": f"Service '{service_id}' not found"},
            status=HTTPStatus.NOT_FOUND,
        )

    del store[service_id]
    log.info("MONITOR.DELETE_SERVICE (id:{})", service_id)
    return web.Response(status=HTTPStatus.NO_CONTENT)


def create_app(
    default_cors_options: CORSOptions,
) -> Tuple[web.Application, Iterable[WebMiddleware]]:
    app = web.Application()
    app["api_versions"] = (1, 2, 3, 4, 5)
    app["prefix"] = "monitor"
    app["monitor.services"] = {}
    cors = aiohttp_cors.setup(app, defaults=default_cors_options)
    services_resource = cors.add(app.router.add_resource("/services"))
    cors.add(services_resource.add_route("GET", list_services))
    cors.add(services_resource.add_route("POST", add_service))
    cors.add(
        app.router.add_route("DELETE", "/services/{service_id}", delete_service)
    )
    return app, []
