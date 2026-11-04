"""Tests for call_notebook_tool, through the fake hub proxy to a real MCP server.

The upstream is a real mcp-SDK server (``tests.fakes.make_notebook_app``)
reached through ``FakeHub``'s ``/user/<name>/<server>/mcp`` route, so the
MCP handshake and tool call run for real; only the network is in-memory.
"""

from __future__ import annotations

import httpx2
import pytest

from af_jupyterhub_mcp.hub.client import HubClient
from af_jupyterhub_mcp.hub.proxy import (
    NotebookToolTransportError,
    NotebookToolUpstreamError,
    call_notebook_tool,
)
from tests.fakes import ALICE_TOKEN, BOB_TOKEN, HUB_URL, FakeHub


async def _call(
    hub: HubClient, tool: str, args: dict[str, object], token: str = ALICE_TOKEN
) -> str:
    async with hub.notebook_http_client(token) as client:
        return await call_notebook_tool(
            http_client=client,
            mcp_url=hub.notebook_mcp_url("alice", "nb-1"),
            tool_name=tool,
            tool_args=args,
        )


class TestCallNotebookTool:
    async def test_returns_the_upstream_text(self, hub: HubClient) -> None:
        text = await _call(hub, "execute_code", {"code": "print(1+1)"})
        assert text == "ran 'print(1+1)' (timeout 30)"

    async def test_requests_carry_the_hub_token_header_not_a_query(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        await _call(hub, "list_files", {"path": "work"})
        proxied = [r for r in fake_hub.requests if r.url.path.startswith("/user/")]
        assert proxied
        for request in proxied:
            assert request.url.path == "/user/alice/nb-1/mcp"
            assert request.headers["authorization"] == f"token {ALICE_TOKEN}"
            assert "token" not in request.url.params

    async def test_upstream_tool_error_is_raised(self, hub: HubClient) -> None:
        with pytest.raises(
            NotebookToolUpstreamError, match="Error executing tool fail"
        ):
            await _call(hub, "fail", {})

    async def test_proxy_refusal_is_a_transport_error(self, hub: HubClient) -> None:
        with pytest.raises(NotebookToolTransportError, match="error response"):
            await _call(hub, "list_files", {}, token=BOB_TOKEN)

    async def test_unreachable_notebook_is_a_transport_error(self) -> None:
        def boom(request: httpx2.Request) -> httpx2.Response:
            msg = "connection refused"
            raise httpx2.ConnectError(msg, request=request)

        hub = HubClient(HUB_URL, transport=httpx2.MockTransport(boom))
        try:
            with pytest.raises(NotebookToolTransportError, match="connection refused"):
                await _call(hub, "list_files", {})
        finally:
            await hub.aclose()

    async def test_errors_never_carry_the_token(self, hub: HubClient) -> None:
        with pytest.raises(NotebookToolTransportError) as excinfo:
            await _call(hub, "list_files", {}, token=BOB_TOKEN)
        assert BOB_TOKEN not in str(excinfo.value)
