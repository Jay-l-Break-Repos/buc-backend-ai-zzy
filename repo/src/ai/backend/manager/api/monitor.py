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

The store is in-memory for now; persistence will be added in a later step.
Polling logic (the 60-second background timer) will also be added in the next step.
"""
from __future__ import annotations

import logging
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING, Iterable, Tuple

import aiohttp_cors
import trafaret as t
from aiohttp import web

from ai.backend.logging import BraceStyleAdapter

from ..models.monitor import InMemoryServiceStore, MonitoredService
from .types import CORSOptions, WebMiddleware

if TYPE_CHECKING:
    pass

log = BraceStyleAdapter(logging.getLogger(__spec__.name))

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
    """
    app = web.Application()
    app["prefix"] = "monitor"
    app["api_versions"] = (4,)
    app["monitor.store"] = InMemoryServiceStore()

    cors = aiohttp_cors.setup(app, defaults=default_cors_options)

    # GET /api/monitor/services  and  POST /api/monitor/services
    services_resource = cors.add(app.router.add_resource("/services"))
    cors.add(services_resource.add_route("GET", list_services))
    cors.add(services_resource.add_route("POST", add_service))

    # DELETE /api/monitor/services/{id}
    service_resource = cors.add(app.router.add_resource(r"/services/{id}"))
    cors.add(service_resource.add_route("DELETE", delete_service))

    return app, []
