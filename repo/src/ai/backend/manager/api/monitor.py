from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, Tuple

import aiohttp
import aiohttp_cors
import attrs
from aiohttp import web

from ai.backend.logging import BraceStyleAdapter

from .types import CORSOptions, WebMiddleware

log = BraceStyleAdapter(logging.getLogger(__spec__.name))

# How often the background poller checks all services (in seconds).
HEALTH_CHECK_INTERVAL: float = 60.0

# Timeout for individual health check HTTP requests (in seconds).
HEALTH_CHECK_TIMEOUT: float = 5.0


@attrs.define(slots=True)
class MonitoredService:
    """Data model for a monitored service endpoint."""

    id: str
    url: str
    name: str
    status: str | None = None  # 'up' or 'down'
    last_check: str | None = None  # ISO-8601 timestamp
    status_code: int | None = None
    latency_ms: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "name": self.name,
            "status": self.status,
            "last_check": self.last_check,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
        }


# In-memory storage for monitored services is stored per-app in app["monitor.services"].


def _get_services_store(app: web.Application) -> dict[str, MonitoredService]:
    """Retrieve the in-memory services store from the app context."""
    return app["monitor.services"]


async def _check_service(
    session: aiohttp.ClientSession,
    service: MonitoredService,
) -> None:
    """Perform a single health check against a monitored service URL."""
    start = time.monotonic()
    try:
        async with session.get(
            service.url,
            timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT),
        ) as resp:
            elapsed_ms = (time.monotonic() - start) * 1000
            service.status_code = resp.status
            service.latency_ms = round(elapsed_ms, 2)
            service.status = "up"
            service.last_check = datetime.now(timezone.utc).isoformat()
    except Exception:
        elapsed_ms = (time.monotonic() - start) * 1000
        service.status = "down"
        service.latency_ms = round(elapsed_ms, 2)
        service.status_code = None
        service.last_check = datetime.now(timezone.utc).isoformat()


async def _poll_services(app: web.Application) -> None:
    """Background task: periodically poll all monitored services."""
    try:
        session = app["monitor.http_session"]
        while True:
            store = _get_services_store(app)
            if store:
                tasks = [
                    _check_service(session, svc) for svc in store.values()
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
    except asyncio.CancelledError:
        pass


async def _on_startup(app: web.Application) -> None:
    """Create the shared HTTP session and start the background poller."""
    app["monitor.http_session"] = aiohttp.ClientSession()
    app["monitor.poll_task"] = asyncio.create_task(_poll_services(app))


async def _on_cleanup(app: web.Application) -> None:
    """Cancel the background poller and close the HTTP session."""
    task: asyncio.Task | None = app.get("monitor.poll_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    session: aiohttp.ClientSession | None = app.get("monitor.http_session")
    if session is not None:
        await session.close()


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

    # Perform an immediate health check so the caller can see results quickly.
    session: aiohttp.ClientSession | None = request.app.get("monitor.http_session")
    if session is not None:
        await _check_service(session, service)

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
    app["prefix"] = "api/monitor"
    app["monitor.services"] = {}
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    cors = aiohttp_cors.setup(app, defaults=default_cors_options)
    services_resource = cors.add(app.router.add_resource("/services"))
    cors.add(services_resource.add_route("GET", list_services))
    cors.add(services_resource.add_route("POST", add_service))
    cors.add(
        app.router.add_route("DELETE", "/services/{service_id}", delete_service)
    )
    return app, []
