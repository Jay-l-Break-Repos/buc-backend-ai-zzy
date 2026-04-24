"""
REST API endpoints for the health check monitor.

Endpoints:
  GET    /api/monitor/services       - list all monitored services with their status
  POST   /api/monitor/services       - add a new URL to monitor
  DELETE /api/monitor/services/{id}  - remove a monitored endpoint

Response format:
  GET  returns: { "services": [ { id, url, name, last_check_time, last_status_code,
                                   last_latency_ms, status, created_at }, ... ] }
  POST returns: { "service": { id, url, name, last_check_time, last_status_code,
                                last_latency_ms, status, created_at } }
  DELETE returns: 204 No Content on success, 404 when not found.

Background polling
------------------
When the sub-app starts a background asyncio task (``_poll_loop``) is launched.
Every ``POLL_INTERVAL_SECONDS`` (default 60) it iterates over every registered
service and performs an HTTP GET using the ``requests`` library inside a thread
executor so the event loop is never blocked.  The result is written back to the
store via ``InMemoryServiceStore.update_health()``.

An immediate first check is also triggered for each newly registered service so
that the caller does not have to wait up to 60 seconds for the first status.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Iterable, Optional, Tuple

import aiohttp_cors
import requests
import trafaret as t
from aiohttp import web

from ai.backend.logging import BraceStyleAdapter

from ..models.monitor import InMemoryServiceStore, MonitoredService
from .types import CORSOptions, WebMiddleware

if TYPE_CHECKING:
    pass

log = BraceStyleAdapter(logging.getLogger(__spec__.name))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: How often (in seconds) the background poller checks all registered services.
POLL_INTERVAL_SECONDS: int = 60

#: Per-request timeout (connect + read) used by the ``requests`` library.
REQUEST_TIMEOUT_SECONDS: float = 10.0

# ---------------------------------------------------------------------------
# Input validation schema
# ---------------------------------------------------------------------------

_add_service_schema = t.Dict(
    {
        t.Key("url"): t.URL,
        t.Key("name", optional=True): t.String(min_length=1, max_length=256),
    }
)

# ---------------------------------------------------------------------------
# Health-check helper
# ---------------------------------------------------------------------------


def _check_service_sync(url: str) -> Tuple[Optional[int], float]:
    """
    Perform a synchronous HTTP GET against *url* and return
    ``(status_code, latency_ms)``.

    This function is intentionally *synchronous* so that it can be safely
    executed inside a :class:`~concurrent.futures.ThreadPoolExecutor` without
    blocking the asyncio event loop.

    If the request fails for any reason (timeout, connection error, etc.) the
    returned ``status_code`` is ``None`` and the latency reflects the time
    elapsed until the failure.
    """
    t0 = time.monotonic()
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
        latency_ms = (time.monotonic() - t0) * 1000.0
        return resp.status_code, latency_ms
    except Exception as exc:
        latency_ms = (time.monotonic() - t0) * 1000.0
        log.debug("Health check failed for {}: {}", url, exc)
        return None, latency_ms


async def _check_service_async(
    app: web.Application,
    service: MonitoredService,
) -> None:
    """
    Run :func:`_check_service_sync` in a thread executor and persist the
    result back to the store.
    """
    store: InMemoryServiceStore = app["monitor.store"]
    executor: ThreadPoolExecutor = app["monitor.executor"]
    loop = asyncio.get_event_loop()

    log.debug("Checking service {} ({})", service.name, service.url)
    try:
        status_code, latency_ms = await loop.run_in_executor(
            executor, _check_service_sync, service.url
        )
    except Exception as exc:
        log.warning("Unexpected error while checking service {}: {}", service.url, exc)
        status_code, latency_ms = None, 0.0

    checked_at = datetime.now(tz=timezone.utc)
    updated = store.update_health(
        service.id,
        status_code=status_code,
        latency_ms=latency_ms,
        checked_at=checked_at,
    )
    if updated:
        log.info(
            "Health check result — service:{} url:{} status:{} latency:{:.1f}ms",
            service.name,
            service.url,
            status_code,
            latency_ms,
        )


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------


async def _poll_loop(app: web.Application) -> None:
    """
    Background asyncio task that polls all registered services every
    :data:`POLL_INTERVAL_SECONDS` seconds.

    The loop sleeps in small increments so that it can be cancelled promptly
    when the application shuts down.
    """
    log.info("Monitor poll loop started (interval={}s)", POLL_INTERVAL_SECONDS)
    try:
        while True:
            # Sleep for POLL_INTERVAL_SECONDS in 1-second ticks so cancellation
            # is responsive.
            for _ in range(POLL_INTERVAL_SECONDS):
                await asyncio.sleep(1)

            store: InMemoryServiceStore = app["monitor.store"]
            services = store.list()
            if not services:
                continue

            log.debug("Poll cycle: checking {} service(s)", len(services))
            # Fan-out: check all services concurrently within this cycle.
            await asyncio.gather(
                *[_check_service_async(app, svc) for svc in services],
                return_exceptions=True,
            )
    except asyncio.CancelledError:
        log.info("Monitor poll loop cancelled — shutting down")
        raise


# ---------------------------------------------------------------------------
# aiohttp lifecycle hooks
# ---------------------------------------------------------------------------


async def _on_startup(app: web.Application) -> None:
    """Start the background thread pool and the polling task."""
    app["monitor.executor"] = ThreadPoolExecutor(
        max_workers=4,
        thread_name_prefix="monitor-check",
    )
    app["monitor.poll_task"] = asyncio.ensure_future(_poll_loop(app))
    log.info("Monitor sub-app started")


async def _on_cleanup(app: web.Application) -> None:
    """Cancel the polling task and shut down the thread pool."""
    poll_task: asyncio.Task = app.get("monitor.poll_task")
    if poll_task is not None and not poll_task.done():
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass

    executor: Optional[ThreadPoolExecutor] = app.get("monitor.executor")
    if executor is not None:
        executor.shutdown(wait=False)

    log.info("Monitor sub-app cleaned up")


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------


async def list_services(request: web.Request) -> web.Response:
    """
    GET /api/monitor/services

    Returns all registered services and their current status as a JSON object
    with a ``"services"`` key containing the list.
    """
    store: InMemoryServiceStore = request.app["monitor.store"]
    log.info("MONITOR.LIST_SERVICES()")
    services = [svc.to_dict() for svc in store.list()]
    return web.json_response({"services": services})


async def add_service(request: web.Request) -> web.Response:
    """
    POST /api/monitor/services

    Registers a new URL to monitor.  The body must be JSON with at least a
    ``"url"`` field.  An optional ``"name"`` field may be provided; when
    omitted the URL itself is used as the display name.

    After the service is persisted an immediate health check is triggered in
    the background so the caller sees a real status on the next GET rather
    than waiting up to 60 seconds.

    Returns 201 with the created service record wrapped in ``{"service": ...}``.
    Returns 400 when the request body is missing or the URL is invalid.
    """
    store: InMemoryServiceStore = request.app["monitor.store"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "Malformed JSON body"},
            status=HTTPStatus.BAD_REQUEST,
        )

    try:
        params = _add_service_schema(body)
    except t.DataError as exc:
        return web.json_response(
            {"error": "Validation failed", "details": exc.as_dict()},
            status=HTTPStatus.BAD_REQUEST,
        )

    url: str = params["url"]
    name: str = params.get("name", url)

    log.info("MONITOR.ADD_SERVICE(name:{}, url:{})", name, url)

    service = MonitoredService.create(url=url, name=name)
    store.add(service)

    # Trigger an immediate first health check without blocking the response.
    asyncio.ensure_future(_check_service_async(request.app, service))

    return web.json_response({"service": service.to_dict()}, status=HTTPStatus.CREATED)


async def delete_service(request: web.Request) -> web.Response:
    """
    DELETE /api/monitor/services/{id}

    Removes the monitored endpoint identified by *id*.

    Returns 204 on success, 400 for a malformed UUID, 404 when not found.
    """
    store: InMemoryServiceStore = request.app["monitor.store"]
    service_id_str = request.match_info.get("id", "")
    log.info("MONITOR.DELETE_SERVICE(id:{})", service_id_str)

    try:
        service_id = uuid.UUID(service_id_str)
    except ValueError:
        return web.json_response(
            {"error": "Invalid service ID format"},
            status=HTTPStatus.BAD_REQUEST,
        )

    removed = store.remove(service_id)
    if not removed:
        return web.json_response(
            {"error": "Service not found"},
            status=HTTPStatus.NOT_FOUND,
        )

    return web.Response(status=HTTPStatus.NO_CONTENT)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    default_cors_options: CORSOptions,
) -> Tuple[web.Application, Iterable[WebMiddleware]]:
    """
    Create and return the ``/api/monitor`` sub-application together with an
    empty middleware list.

    The in-memory service store is attached to the application under the key
    ``"monitor.store"`` so that it can be injected or replaced in tests.

    A background polling task is registered via ``on_startup`` / ``on_cleanup``
    lifecycle hooks.
    """
    app = web.Application()
    app["prefix"] = "monitor"
    app["api_versions"] = (4,)
    app["monitor.store"] = InMemoryServiceStore()

    # Register lifecycle hooks for the background poller.
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    cors = aiohttp_cors.setup(app, defaults=default_cors_options)

    # GET /api/monitor/services  and  POST /api/monitor/services
    services_resource = cors.add(app.router.add_resource("/services"))
    cors.add(services_resource.add_route("GET", list_services))
    cors.add(services_resource.add_route("POST", add_service))

    # DELETE /api/monitor/services/{id}
    service_resource = cors.add(app.router.add_resource(r"/services/{id}"))
    cors.add(service_resource.add_route("DELETE", delete_service))

    return app, []
