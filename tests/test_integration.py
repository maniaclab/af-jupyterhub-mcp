"""Integration tests: the real app driven by a real MCP client, against the fake hub.

Nothing inside the app is patched: requests go through the SDK's bearer
middleware, the HubTokenVerifier, the tools, HubClient, and the fake hub's
proxy into a real mcp-SDK server standing in for jupyter-mcp-server. Only
the network is in-memory (httpx2 ASGI/Mock transports).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from af_jupyterhub_mcp.server import make_app
from tests.conftest import CPU_IMAGE, PUBLIC_HUB_URL
from tests.fakes import ALICE_TOKEN, FakeHub
from tests.test_server import RESOURCE_URL, TOOL_COUNT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from af_jupyterhub_mcp.config import Settings


@asynccontextmanager
async def mcp_session(
    settings: Settings, fake_hub: FakeHub, token: str = ALICE_TOKEN
) -> AsyncIterator[ClientSession]:
    """Run the app against *fake_hub* and yield an initialized client session."""
    app = make_app(
        settings=settings,
        resource_url=RESOURCE_URL,
        host="127.0.0.1",
        transport=fake_hub.transport(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=RESOURCE_URL,
            headers={"Authorization": f"Bearer {token}"},
        ) as http_client,
        streamable_http_client(f"{RESOURCE_URL}/mcp", http_client=http_client) as (
            read_stream,
            write_stream,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


def _text(result: Any) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


class TestJupyterHubMcpOverStreamableHttp:
    async def test_tools_list(self, settings: Settings, fake_hub: FakeHub) -> None:
        async with mcp_session(settings, fake_hub) as session:
            listed = await session.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        assert len(tools) == TOOL_COUNT
        for tool in tools.values():
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is not None
            assert tool.output_schema is not None
        assert tools["delete_jupyter_server"].annotations.destructive_hint is True  # type: ignore[union-attr]
        assert tools["list_sites"].annotations.read_only_hint is True  # type: ignore[union-attr]

    async def test_launch_then_execute_then_delete(
        self, settings: Settings, fake_hub: FakeHub
    ) -> None:
        seen: list[tuple[float, float | None, str | None]] = []

        async def on_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            seen.append((progress, total, message))

        async with mcp_session(settings, fake_hub) as session:
            created = await session.call_tool(
                "create_jupyter_server",
                {"image": CPU_IMAGE},
                progress_callback=on_progress,
            )
            assert created.is_error is not True, _text(created)
            server = (created.structured_content or {})["server"]
            assert server["name"] == "notebook-1"
            assert server["url"] == f"{PUBLIC_HUB_URL}/user/alice/notebook-1/"

            executed = await session.call_tool(
                "nb_execute_code", {"server_name": "notebook-1", "code": "print(1+1)"}
            )
            assert _text(executed) == "ran 'print(1+1)' (timeout 30)"

            deleted = await session.call_tool(
                "delete_jupyter_server", {"name": "notebook-1"}
            )
            assert deleted.structured_content == {"name": "notebook-1", "deleted": True}

        assert [p[0] for p in seen] == [10, 50, 100]
        assert {total for _, total, _ in seen} == {100}
        assert seen[1][2] == "Pulling image"
        assert fake_hub.servers["alice"] == {}
        for result in (created, executed, deleted):
            assert ALICE_TOKEN not in _text(result)
            assert ALICE_TOKEN not in json.dumps(result.structured_content)

    async def test_binderhub_ui_server_is_listed_without_its_token(
        self, settings: Settings, fake_hub: FakeHub
    ) -> None:
        fake_hub.add_server(
            "alice",
            "maniaclab-ml_platform-ab12cd34",
            user_options={
                "token": "SECRET-binder-token",
                "image": "harbor.af.uchicago.edu/binderhub/ml-platform:abc",
                "binder_request": "v2/gh/maniaclab/ml_platform/main",
            },
        )
        async with mcp_session(settings, fake_hub) as session:
            listed = await session.call_tool("list_jupyter_servers", {})
        servers = (listed.structured_content or {})["servers"]
        assert [s["name"] for s in servers] == ["maniaclab-ml_platform-ab12cd34"]
        assert "SECRET-binder-token" not in _text(listed)
        assert "SECRET-binder-token" not in json.dumps(listed.structured_content)

    async def test_token_missing_scopes_gets_a_clear_tool_error(
        self, settings: Settings, fake_hub: FakeHub
    ) -> None:
        fake_hub.tokens["narrow"] = ("alice", ["read:users:name!user=alice"])
        async with mcp_session(settings, fake_hub, token="narrow") as session:
            result = await session.call_tool("list_jupyter_servers", {})
        assert result.is_error is True
        assert "servers!user" in _text(result)
        assert "re-link" in _text(result)

    async def test_identity_lookup_is_cached_across_calls(
        self, settings: Settings, fake_hub: FakeHub
    ) -> None:
        async with mcp_session(settings, fake_hub) as session:
            await session.call_tool("list_jupyter_servers", {})
            await session.call_tool("list_jupyter_servers", {})
        whoami = [r for r in fake_hub.requests if r.url.path == "/hub/api/user"]
        assert len(whoami) == 1
