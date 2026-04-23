"""
REST API endpoints for the health check monitor.

Endpoints:
  GET  /api/monitor/services         - list all monitored services with their status
  POST /api/monitor/services         - add a new URL to monitor
  DELETE /api/monitor/services/{id}  - remove a monitored endpoint

Response format (matching test expectations):
  POST returns: { id, name, url, last_check, status_code, latency_ms, status, created_at }
  GET  returns: [ { id, name, url, last_check, status_code, latency_ms, status, created_at }, ... ]

Polling:
  A background task polls every registered service every 60 seconds.
  When a new service is added, it is polled immediately (within 1 second).
  Poll results update last_check, status_code, latency_ms, and status fields.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Iterable, Optional, Set, Tuple

import aiohttp_cors
import attrs
import requests
import sqlalchemy as sa
import trafaret as t
from aiohttp import web

from ai.backend.logging import BraceStyleAdapter

from ..models.monitor import MonitoredServiceRow, monitored_services
from .types import CORSOptions, WebMiddleware
from .utils import check_api_params

if TYPE_CHECKING:
    from .context import RootContext

log = BraceStyleAdapter(logging.getLogger(__spec__.name))

# How often (seconds) to poll all services in the background loop
POLL_INTERVAL_SECONDS = 60

# HTTP request timeout for each health check
POLL_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Polling helpers
# ---------------------------------------------------------------------------

def _do_requests_get(url: str) -> Tuple[int, float, str]:
    """
    Synchronous helper that calls requests.get() and returns
    (status_code, latency_ms, status).  Runs inside a thread executor.
    """
    t0 = time.monotonic()
    resp = requests.get(url, timeout=POLL_TIMEOUT_SECONDS, allow_redirects=True)
    elapsed = time.monotonic() - t0
    latency_ms = round(elapsed * 1000, 2)
    status = "up" if resp.status_code < 400 else "down"
    return resp.status_code, latency_ms, status


async def _poll_one_service(
    root_ctx: RootContext,
    service_id: uuid.UUID,
    url: str,
) -> None:
    """
    Perform a single HTTP GET against *url* using requests.get() (run in a
    thread executor so the async event loop is not blocked), then write the
    result back to the database row identified by *service_id*.
    """
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    status: str = "down"

    try:
        loop = asyncio.get_event_loop()
        status_code, latency_ms, status = await loop.run_in_executor(
            None, lambda: _do_requests_get(url)
        )
    except Exception as exc:
        log.debug("MONITOR.POLL_ONE_SERVICE: url={} error={}", url, exc)
        status = "down"

    now = datetime.now(tz=timezone.utc)
    try:
        async with root_ctx.db.begin() as conn:
            update_query = (
                monitored_services.update()
                .where(monitored_services.c.id == service_id)
                .values(
                    last_check=now,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    status=status,
                )
            )
            await conn.execute(update_query)
    except Exception as exc:
        log.exception("MONITOR.POLL_ONE_SERVICE: db update failed for id={}: {}", service_id, exc)


async def _poll_all_services(root_ctx: RootContext) -> None:
    """
    Fetch all registered services and poll each one concurrently.
    """
    try:
        async with root_ctx.db.begin_readonly() as conn:
            query = sa.select(monitored_services.c.id, monitored_services.c.url)
            result = await conn.execute(query)
            rows = result.fetchall()
    except Exception as exc:
        log.exception("MONITOR.POLL_ALL_SERVICES: failed to fetch services: {}", exc)
        return

    if not rows:
        return

    tasks = [
        asyncio.create_task(_poll_one_service(root_ctx, row["id"], row["url"]))
        for row in rows
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _background_poll_loop(
    root_ctx: RootContext,
    pending_ids: Set[uuid.UUID],
) -> None:
    """
    Background loop that:
      1. Immediately polls any service IDs in *pending_ids* (newly added).
      2. Every POLL_INTERVAL_SECONDS, polls all registered services.
    """
    next_full_poll = time.monotonic() + POLL_INTERVAL_SECONDS
    try:
        while True:
            # Poll any newly-added services right away
            if pending_ids:
                ids_to_poll = list(pending_ids)
                pending_ids.clear()
                try:
                    async with root_ctx.db.begin_readonly() as conn:
                        query = sa.select(
                            monitored_services.c.id, monitored_services.c.url
                        ).where(monitored_services.c.id.in_(ids_to_poll))
                        result = await conn.execute(query)
                        rows = result.fetchall()
                except Exception as exc:
                    log.exception(
                        "MONITOR.BACKGROUND_POLL_LOOP: failed to fetch pending services: {}", exc
                    )
                    rows = []

                if rows:
                    tasks = [
                        asyncio.create_task(
                            _poll_one_service(root_ctx, row["id"], row["url"])
                        )
                        for row in rows
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)

            # Full periodic poll
            now = time.monotonic()
            if now >= next_full_poll:
                await _poll_all_services(root_ctx)
                next_full_poll = time.monotonic() + POLL_INTERVAL_SECONDS

            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Private context (background task handle + pending-poll set)
# ---------------------------------------------------------------------------

@attrs.define(slots=True, auto_attribs=True)
class PrivateContext:
    poll_task: Optional[asyncio.Task] = None
    pending_ids: Set[uuid.UUID] = attrs.Factory(set)


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

async def list_services(request: web.Request) -> web.Response:
    """
    GET /api/monitor/services

    Returns a flat array of all monitored services with their current status.
    """
    root_ctx: RootContext = request.app["_root.context"]
    log.info("MONITOR.LIST_SERVICES()")

    async with root_ctx.db.begin_readonly() as conn:
        query = sa.select(monitored_services).order_by(monitored_services.c.created_at.asc())
        result = await conn.execute(query)
        rows = result.fetchall()

    services = [MonitoredServiceRow.from_row(row).to_dict() for row in rows]
    return web.json_response(services)


@check_api_params(
    t.Dict({
        t.Key("name"): t.String,
        t.Key("url"): t.URL,
    })
)
async def add_service(request: web.Request, params: Any) -> web.Response:
    """
    POST /api/monitor/services

    Adds a new URL to monitor. The service is saved and immediately queued
    for its first health check poll.
    Returns the created service record (flat, not nested).
    """
    root_ctx: RootContext = request.app["_root.context"]
    app_ctx: PrivateContext = request.app["monitor.context"]
    name = str(params["name"])
    url = str(params["url"])
    log.info("MONITOR.ADD_SERVICE(name:{}, url:{})", name, url)

    new_id = uuid.uuid4()
    async with root_ctx.db.begin() as conn:
        insert_query = monitored_services.insert().values(
            id=new_id,
            name=name,
            url=url,
            last_check=None,
            status_code=None,
            latency_ms=None,
            status=None,
        )
        await conn.execute(insert_query)

        # Fetch the newly created row
        select_query = sa.select(monitored_services).where(monitored_services.c.id == new_id)
        result = await conn.execute(select_query)
        row = result.first()

    if row is None:
        return web.json_response(
            {"error": "Failed to create service"},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    # Queue the new service for an immediate poll
    app_ctx.pending_ids.add(new_id)

    service = MonitoredServiceRow.from_row(row).to_dict()
    # Return the service object directly (flat), not nested in {"service": ...}
    return web.json_response(service, status=HTTPStatus.CREATED)


async def delete_service(request: web.Request) -> web.Response:
    """
    DELETE /api/monitor/services/{id}

    Removes a monitored endpoint by its ID.
    """
    root_ctx: RootContext = request.app["_root.context"]
    service_id_str = request.match_info.get("id", "")
    log.info("MONITOR.DELETE_SERVICE(id:{})", service_id_str)

    try:
        service_id = uuid.UUID(service_id_str)
    except ValueError:
        return web.json_response(
            {"error": "Invalid service ID format"},
            status=HTTPStatus.BAD_REQUEST,
        )

    async with root_ctx.db.begin() as conn:
        delete_query = monitored_services.delete().where(monitored_services.c.id == service_id)
        result = await conn.execute(delete_query)

    if result.rowcount == 0:
        return web.json_response(
            {"error": "Service not found"},
            status=HTTPStatus.NOT_FOUND,
        )

    return web.Response(status=HTTPStatus.NO_CONTENT)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def init(app: web.Application) -> None:
    root_ctx: RootContext = app["_root.context"]
    app_ctx: PrivateContext = app["monitor.context"]
    # pending_ids is already initialized to an empty set by PrivateContext()
    app_ctx.poll_task = asyncio.create_task(
        _background_poll_loop(root_ctx, app_ctx.pending_ids)
    )
    log.info("MONITOR: background health-check polling started (interval={}s)", POLL_INTERVAL_SECONDS)


async def shutdown(app: web.Application) -> None:
    app_ctx: PrivateContext = app["monitor.context"]
    if app_ctx.poll_task is not None:
        app_ctx.poll_task.cancel()
        await asyncio.sleep(0)
        if not app_ctx.poll_task.done():
            try:
                await app_ctx.poll_task
            except asyncio.CancelledError:
                pass
    log.info("MONITOR: background health-check polling stopped")


def create_app(
    default_cors_options: CORSOptions,
) -> Tuple[web.Application, Iterable[WebMiddleware]]:
    app = web.Application()
    app["prefix"] = "monitor"
    app["api_versions"] = (4,)
    app["monitor.context"] = PrivateContext()
    cors = aiohttp_cors.setup(app, defaults=default_cors_options)

    # GET /api/monitor/services  and  POST /api/monitor/services
    services_resource = cors.add(app.router.add_resource("/services"))
    cors.add(services_resource.add_route("GET", list_services))
    cors.add(services_resource.add_route("POST", add_service))

    # DELETE /api/monitor/services/{id}
    service_resource = cors.add(app.router.add_resource(r"/services/{id}"))
    cors.add(service_resource.add_route("DELETE", delete_service))

    app.on_startup.append(init)
    app.on_shutdown.append(shutdown)

    return app, []
