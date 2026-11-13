"""MCP server setup for af-jupyterhub-mcp.

HTTP transport only (no stdio): this backend always runs behind the
af-mcp-platform aggregator, which forwards each user's linked JupyterHub
token as the bearer -- there is no local single-user CLI use case.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse

from af_jupyterhub_mcp.auth.hub import HubTokenVerifier
from af_jupyterhub_mcp.config import Settings
from af_jupyterhub_mcp.hub.client import HubClient
from af_jupyterhub_mcp.hub.errors import HubUnavailableError
from af_jupyterhub_mcp.tools import jupyterhub as jupyterhub_tools
from af_jupyterhub_mcp.tools import nb_proxy as nb_proxy_tools
from af_jupyterhub_mcp.tools import nb_ui as nb_ui_tools

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import httpx2
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "MCP server for per-user JupyterHub servers on the ATLAS Analysis "
    "Facility. Provides tools to start, inspect, stop, and delete your own "
    "named JupyterHub servers (including ones launched from BinderHub), and "
    "to list the supported images and sites. The nb_* tools read, edit, and "
    "execute notebooks inside a running server; the nb_ui_* tools drive the "
    "JupyterLab UI and need the user's JupyterLab tab open. Every server is "
    "scoped to your own JupyterHub identity -- there is no way to act on "
    "another user's server."
)


class _HubUnavailableMiddleware:
    """Answer 503 when the transport's token check could not reach the hub.

    Wraps the SDK's authentication middleware. Without it the verifier's
    ``HubUnavailableError`` would escape as a bare 500 with a traceback; and
    it must never become a 401, which af-mcp-platform reads as a dead token.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except HubUnavailableError as exc:
            log.warning("Token check failed, JupyterHub unavailable: %s", exc)
            response = JSONResponse(
                {"error": "temporarily_unavailable", "error_description": str(exc)},
                status_code=503,
            )
            await response(scope, receive, send)


def _register_all(mcp: MCPServer) -> None:
    """Register every tool module on *mcp*."""
    jupyterhub_tools.register(mcp)
    nb_proxy_tools.register(mcp)
    nb_ui_tools.register(mcp)


def make_app(
    *,
    settings: Settings,
    resource_url: str,
    host: str,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Starlette:
    """Build the ASGI app for HTTP transport behind af-mcp-platform.

    *transport* replaces the network for every hub call (tests pass the
    fake hub's MockTransport); production leaves it None.
    """
    hub = HubClient(settings.hub_url, transport=transport)
    verifier = HubTokenVerifier(hub, ttl_seconds=settings.auth_cache_ttl_seconds)

    @asynccontextmanager
    async def _lifespan(_server: MCPServer) -> AsyncGenerator[dict[str, Any], None]:
        try:
            yield {"settings": settings, "hub": hub, "verifier": verifier}
        finally:
            await hub.aclose()

    mcp = MCPServer(
        "af-jupyterhub-mcp",
        instructions=_INSTRUCTIONS,
        lifespan=_lifespan,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(resource_url),
            # The aggregator injects the bearer itself; no OAuth discovery
            # chain to advertise on this resource.
            resource_server_url=None,
            client_registration_options=ClientRegistrationOptions(enabled=False),
            required_scopes=[],
        ),
    )
    _register_all(mcp)

    async def _healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    mcp.custom_route("/healthz", methods=["GET"])(_healthz)

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", host=host, stateless_http=True
    )
    # Outermost, so it also wraps the SDK's authentication middleware.
    app.add_middleware(_HubUnavailableMiddleware)
    return app


def serve(
    host: str = "0.0.0.0",
    port: int = 8000,
    resource_url: str | None = None,
    forwarded_allow_ips: str = "127.0.0.1",
    log_level: str = "info",
) -> None:
    """Start the MCP server over HTTP transport, behind af-mcp-platform."""
    settings = Settings.from_env()
    app = make_app(
        settings=settings,
        resource_url=resource_url or f"http://{host}:{port}",
        host=host,
    )

    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        log_level=log_level,
    )
