"""Tests for HubClient against the in-memory FakeHub (no network)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx2
import pytest

from af_jupyterhub_mcp.hub.client import HubClient, parse_progress_lines
from af_jupyterhub_mcp.hub.errors import HubAPIError, HubRequestError
from tests.fakes import ALICE_TOKEN, HUB_URL, FakeHub

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _lines(*lines: str) -> AsyncIterator[str]:
    for line in lines:
        yield line


class TestParseProgressLines:
    async def test_yields_each_data_event(self) -> None:
        events = [
            e
            async for e in parse_progress_lines(
                _lines('data: {"progress": 10}', "", 'data: {"ready": true}', "")
            )
        ]
        assert events == [{"progress": 10}, {"ready": True}]

    async def test_skips_keepalives_and_comments(self) -> None:
        events = [
            e
            async for e in parse_progress_lines(
                _lines("", "", ": keepalive", "event: message", 'data:{"x": 1}')
            )
        ]
        assert events == [{"x": 1}]

    async def test_malformed_json_is_a_hub_api_error(self) -> None:
        with pytest.raises(HubAPIError, match="malformed"):
            _ = [e async for e in parse_progress_lines(_lines("data: {nope"))]

    async def test_non_object_payload_is_a_hub_api_error(self) -> None:
        with pytest.raises(HubAPIError, match="malformed"):
            _ = [e async for e in parse_progress_lines(_lines("data: [1, 2]"))]


class TestAuthHeader:
    async def test_every_call_sends_the_token_scheme(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        await hub.get_self(ALICE_TOKEN)
        await hub.get_user(ALICE_TOKEN, "alice")
        assert all(
            r.headers["authorization"] == f"token {ALICE_TOKEN}"
            for r in fake_hub.requests
        )


class TestGetSelf:
    async def test_returns_name_and_scopes(self, hub: HubClient) -> None:
        model = await hub.get_self(ALICE_TOKEN)
        assert model["name"] == "alice"
        assert "servers!user=alice" in model["scopes"]

    async def test_bad_token_raises_with_hub_message(self, hub: HubClient) -> None:
        with pytest.raises(HubAPIError) as excinfo:
            await hub.get_self("nope")
        assert excinfo.value.status == 403
        assert "Missing or invalid credentials" in str(excinfo.value)
        assert "nope" not in str(excinfo.value)

    async def test_transport_failure_is_a_hub_request_error(self) -> None:
        def boom(request: httpx2.Request) -> httpx2.Response:
            msg = "no route"
            raise httpx2.ConnectError(msg, request=request)

        client = HubClient(HUB_URL, transport=httpx2.MockTransport(boom))
        try:
            with pytest.raises(HubRequestError, match="no route"):
                await client.get_self(ALICE_TOKEN)
        finally:
            await client.aclose()

    async def test_non_json_error_body_falls_back_to_reason(self) -> None:
        client = HubClient(
            HUB_URL,
            transport=httpx2.MockTransport(
                lambda _r: httpx2.Response(502, text="<html>")
            ),
        )
        try:
            with pytest.raises(HubAPIError) as excinfo:
                await client.get_self(ALICE_TOKEN)
        finally:
            await client.aclose()
        assert excinfo.value.status == 502
        assert "Bad Gateway" in str(excinfo.value)


class TestGetUser:
    async def test_includes_stopped_servers(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        fake_hub.add_server("alice", "running")
        fake_hub.add_server("alice", "stopped", ready=False)
        model = await hub.get_user(ALICE_TOKEN, "alice")
        assert set(model["servers"]) == {"running", "stopped"}
        assert fake_hub.requests[-1].url.params["include_stopped_servers"] == "1"


class TestStartServer:
    async def test_posts_user_options_and_returns_status(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        options = {"image": "img:1", "resource_requests": {"cpu": "1"}}
        status = await hub.start_server(ALICE_TOKEN, "alice", "nb-1", options)
        assert status == 202
        request = fake_hub.requests[-1]
        assert request.method == "POST"
        assert request.url.path == "/hub/api/users/alice/servers/nb-1"
        assert json.loads(request.content) == options

    async def test_named_server_limit_raises_400(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        fake_hub.named_server_limit = 1
        fake_hub.add_server("alice", "nb-1")
        with pytest.raises(HubAPIError) as excinfo:
            await hub.start_server(ALICE_TOKEN, "alice", "nb-2", {})
        assert excinfo.value.status == 400
        assert "maximum of 1 named servers" in str(excinfo.value)

    async def test_server_name_is_url_quoted(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        await hub.start_server(ALICE_TOKEN, "alice", "a b", {})
        assert fake_hub.requests[-1].url.raw_path.startswith(
            b"/hub/api/users/alice/servers/a%20b"
        )


class TestProgressEvents:
    async def test_streams_events_until_ready(self, hub: HubClient) -> None:
        await hub.start_server(ALICE_TOKEN, "alice", "nb-1", {})
        events = [e async for e in hub.progress_events(ALICE_TOKEN, "alice", "nb-1")]
        assert [e.get("progress") for e in events] == [10, 50, 100]
        assert events[-1]["ready"] is True
        assert events[-1]["url"] == "/user/alice/nb-1/"

    async def test_missing_server_raises_404(self, hub: HubClient) -> None:
        with pytest.raises(HubAPIError) as excinfo:
            _ = [e async for e in hub.progress_events(ALICE_TOKEN, "alice", "nope")]
        assert excinfo.value.status == 404


class TestStopServer:
    async def test_stop_keeps_the_entry(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        fake_hub.add_server("alice", "nb-1")
        status = await hub.stop_server(ALICE_TOKEN, "alice", "nb-1", remove=False)
        assert status == 204
        assert fake_hub.servers["alice"]["nb-1"]["stopped"] is True
        assert json.loads(fake_hub.requests[-1].content) == {"remove": False}

    async def test_remove_deletes_the_entry(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        fake_hub.add_server("alice", "nb-1")
        await hub.stop_server(ALICE_TOKEN, "alice", "nb-1", remove=True)
        assert "nb-1" not in fake_hub.servers["alice"]
        assert fake_hub.requests[-1].method == "DELETE"
        assert json.loads(fake_hub.requests[-1].content) == {"remove": True}

    async def test_missing_server_raises_404(self, hub: HubClient) -> None:
        with pytest.raises(HubAPIError) as excinfo:
            await hub.stop_server(ALICE_TOKEN, "alice", "nope", remove=True)
        assert excinfo.value.status == 404


class TestNotebookEndpoint:
    def test_mcp_url_goes_through_the_hub_proxy(self, hub: HubClient) -> None:
        assert (
            hub.notebook_mcp_url("alice", "nb 1") == f"{HUB_URL}/user/alice/nb%201/mcp"
        )

    async def test_notebook_client_carries_the_token_header(
        self, hub: HubClient
    ) -> None:
        async with hub.notebook_http_client(ALICE_TOKEN) as client:
            assert client.headers["authorization"] == f"token {ALICE_TOKEN}"
