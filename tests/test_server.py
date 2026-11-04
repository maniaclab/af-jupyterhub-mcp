"""Tests for the ASGI app: tool registration, health check, and transport auth."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx2
from mcp.server.mcpserver import MCPServer

from af_jupyterhub_mcp.server import _register_all, make_app
from tests.fakes import ALICE_TOKEN, FakeHub

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from af_jupyterhub_mcp.config import Settings

RESOURCE_URL = "http://127.0.0.1:8000"
TOOL_COUNT = 76

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@asynccontextmanager
async def app_client(
    settings: Settings, fake_hub: FakeHub
) -> AsyncIterator[httpx2.AsyncClient]:
    """Run the real app (lifespan included) against *fake_hub*, in-process."""
    app = make_app(
        settings=settings,
        resource_url=RESOURCE_URL,
        host="127.0.0.1",
        transport=fake_hub.transport(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=RESOURCE_URL
        ) as client,
    ):
        yield client


class TestEveryToolDeclaresAnnotationsAndOutputSchema:
    """Drift guard: every registered tool must publish annotations and an outputSchema.

    This is the one test that would catch a future tool being added (or an
    existing one refactored) without following the
    ``Annotated[CallToolResult, Model]`` + ``ToolAnnotations`` pattern all
    76 tools use today -- see CLAUDE.md's "Tool registration pattern".
    """

    def test_every_tool_declares_annotations_and_output_schema(self) -> None:
        mcp = MCPServer("test")
        _register_all(mcp)
        tools = mcp._tool_manager.list_tools()
        assert len(tools) == TOOL_COUNT, [t.name for t in tools]
        for tool in tools:
            assert tool.annotations is not None, tool.name
            assert tool.annotations.read_only_hint is not None, tool.name
            assert tool.output_schema is not None, tool.name


class TestApp:
    async def test_healthz_needs_no_auth(
        self, settings: Settings, fake_hub: FakeHub
    ) -> None:
        async with app_client(settings, fake_hub) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert fake_hub.requests == []

    async def test_initialize_without_token_is_401(
        self, settings: Settings, fake_hub: FakeHub
    ) -> None:
        async with app_client(settings, fake_hub) as client:
            response = await client.post("/mcp", json=_INITIALIZE, headers=_MCP_HEADERS)
        assert response.status_code == 401

    async def test_initialize_with_unknown_token_is_401(
        self, settings: Settings, fake_hub: FakeHub
    ) -> None:
        async with app_client(settings, fake_hub) as client:
            response = await client.post(
                "/mcp",
                json=_INITIALIZE,
                headers={**_MCP_HEADERS, "Authorization": "Bearer wrong"},
            )
        assert response.status_code == 401
        assert fake_hub.requests[-1].url.path == "/hub/api/user"

    async def test_initialize_with_hub_token(
        self, settings: Settings, fake_hub: FakeHub
    ) -> None:
        async with app_client(settings, fake_hub) as client:
            response = await client.post(
                "/mcp",
                json=_INITIALIZE,
                headers={**_MCP_HEADERS, "Authorization": f"Bearer {ALICE_TOKEN}"},
            )
        assert response.status_code == 200
        assert "serverInfo" in response.text
