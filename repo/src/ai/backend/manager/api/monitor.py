"""
REST API endpoints for the health check monitor.

Endpoints:
  GET  /api/monitor/services         - list all monitored services with their status
  POST /api/monitor/services         - add a new URL to monitor
  DELETE /api/monitor/services/{id}  - remove a monitored endpoint
"""
from __future__ import annotations

import logging
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Iterable, Tuple

import aiohttp_cors
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


async def list_services(request: web.Request) -> web.Response:
    """
    GET /api/monitor/services

    Returns a list of all monitored services with their current status.
    """
    root_ctx: RootContext = request.app["_root.context"]
    log.info("MONITOR.LIST_SERVICES()")

    async with root_ctx.db.begin_readonly() as conn:
        query = sa.select(monitored_services).order_by(monitored_services.c.created_at.asc())
        result = await conn.execute(query)
        rows = result.fetchall()

    services = [MonitoredServiceRow.from_row(row).to_dict() for row in rows]
    return web.json_response({"services": services})


@check_api_params(
    t.Dict({
        t.Key("url"): t.URL,
    })
)
async def add_service(request: web.Request, params: Any) -> web.Response:
    """
    POST /api/monitor/services

    Adds a new URL to monitor. The service is saved but not polled yet.
    Returns the created service record.
    """
    root_ctx: RootContext = request.app["_root.context"]
    url = str(params["url"])
    log.info("MONITOR.ADD_SERVICE(url:{})", url)

    new_id = uuid.uuid4()
    async with root_ctx.db.begin() as conn:
        insert_query = monitored_services.insert().values(
            id=new_id,
            url=url,
            last_check_time=None,
            last_status_code=None,
            last_latency_ms=None,
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

    service = MonitoredServiceRow.from_row(row).to_dict()
    return web.json_response({"service": service}, status=HTTPStatus.CREATED)


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


def create_app(
    default_cors_options: CORSOptions,
) -> Tuple[web.Application, Iterable[WebMiddleware]]:
    app = web.Application()
    app["prefix"] = "monitor"
    app["api_versions"] = (4,)
    cors = aiohttp_cors.setup(app, defaults=default_cors_options)

    # GET /api/monitor/services  and  POST /api/monitor/services
    services_resource = cors.add(app.router.add_resource("/services"))
    cors.add(services_resource.add_route("GET", list_services))
    cors.add(services_resource.add_route("POST", add_service))

    # DELETE /api/monitor/services/{id}
    service_resource = cors.add(app.router.add_resource(r"/services/{id}"))
    cors.add(service_resource.add_route("DELETE", delete_service))

    return app, []
