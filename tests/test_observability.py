"""Observability contract (af-mcp-platform docs/mcp-design.md L1-L3).

The app tests drive the real ASGI app (SDK bearer middleware, HubTokenVerifier,
tools, HubClient) against the fake hub. Only the network is in-memory, so a
request id that reaches the tool-call line, or the verifier's own warning,
really crossed the HTTP edge.
"""

from __future__ import annotations

import io
import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from af_jupyterhub_mcp.observability import (
    JsonFormatter,
    RequestLogMiddleware,
    configure_logging,
    request_id_var,
)
from af_jupyterhub_mcp.server import make_app
from tests.fakes import ALICE_TOKEN, FakeHub
from tests.test_server import RESOURCE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from af_jupyterhub_mcp.config import Settings

JSON_RPC_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def log_lines() -> Iterator[io.StringIO]:
    """Route every log record to a buffer as JSON, restoring the root logger after."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    buffer = io.StringIO()
    configure_logging("info", stream=buffer)
    yield buffer
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def parsed(buffer: io.StringIO) -> list[dict[str, Any]]:
    """Return the request's log lines: the access line is the last one it writes.

    Anything after it is the in-process test client's own logging, not the server's.
    """
    lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line]
    ends = [i for i, line in enumerate(lines) if line["event"] == "http.request"]
    return lines[: ends[-1] + 1] if ends else lines


@asynccontextmanager
async def http_client(
    settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
) -> AsyncIterator[httpx2.AsyncClient]:
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
        # Drop the lifespan's startup lines: they belong to no request.
        log_lines.seek(0)
        log_lines.truncate()
        yield client


async def call_tool(
    client: httpx2.AsyncClient,
    name: str,
    arguments: dict[str, Any],
    *,
    token: str = ALICE_TOKEN,
    request_id: str | None = "abc",
) -> httpx2.Response:
    headers = {**JSON_RPC_HEADERS, "Authorization": f"Bearer {token}"}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers,
    )


def result_of(response: httpx2.Response) -> dict[str, Any]:
    # The stateless transport answers as a server-sent event.
    (data,) = [x for x in response.text.splitlines() if x.startswith("data: ")]
    return json.loads(data.removeprefix("data: "))["result"]  # type: ignore[no-any-return]


class TestJsonFormatter:
    def test_standard_fields_and_request_id(self) -> None:
        record = logging.LogRecord(
            "lg", logging.WARNING, __file__, 1, "evt", None, None
        )
        record.fields = {"tool": "list_sites", "duration_ms": 1.5}
        reset = request_id_var.set("rid-7")
        try:
            entry = json.loads(JsonFormatter().format(record))
        finally:
            request_id_var.reset(reset)
        assert entry["level"] == "warning"
        assert entry["logger"] == "lg"
        assert entry["event"] == "evt"
        assert entry["request_id"] == "rid-7"
        assert entry["tool"] == "list_sites"
        assert entry["timestamp"].endswith("+00:00")


class TestRequestId:
    async def test_every_line_of_a_request_carries_the_brokers_id(
        self, settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
    ) -> None:
        async with http_client(settings, fake_hub, log_lines) as client:
            resp = await call_tool(client, "list_jupyter_servers", {})
        assert resp.status_code == 200
        lines = parsed(log_lines)
        assert {"tool.call", "http.request"} <= {line["event"] for line in lines}
        assert all(line.get("request_id") == "abc" for line in lines)

    async def test_a_line_written_by_the_token_check_carries_the_id(
        self, settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
    ) -> None:
        async with http_client(settings, fake_hub, log_lines) as client:
            resp = await call_tool(client, "list_jupyter_servers", {}, token="bogus")
        assert resp.status_code == 401
        lines = parsed(log_lines)
        assert any(line["level"] == "warning" for line in lines)
        assert all(line.get("request_id") == "abc" for line in lines)

    async def test_an_id_is_generated_when_the_header_is_absent(
        self, settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
    ) -> None:
        async with http_client(settings, fake_hub, log_lines) as client:
            await call_tool(client, "list_jupyter_servers", {}, request_id=None)
        ids = {line["request_id"] for line in parsed(log_lines)}
        assert len(ids) == 1
        assert ids.pop()

    async def test_an_overlong_id_is_truncated(
        self, settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
    ) -> None:
        async with http_client(settings, fake_hub, log_lines) as client:
            await call_tool(client, "list_jupyter_servers", {}, request_id="x" * 500)
        assert {len(line["request_id"]) for line in parsed(log_lines)} == {128}


class TestToolCallLine:
    async def test_carries_the_contract_fields(
        self, settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
    ) -> None:
        async with http_client(settings, fake_hub, log_lines) as client:
            await call_tool(client, "list_jupyter_servers", {})
        (line,) = [x for x in parsed(log_lines) if x["event"] == "tool.call"]
        assert line["tool"] == "list_jupyter_servers"
        assert line["subject"] == "alice"
        assert line["outcome"] == "ok"
        assert line["duration_ms"] >= 0

    async def test_a_tool_error_result_is_outcome_error(
        self, settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
    ) -> None:
        async with http_client(settings, fake_hub, log_lines) as client:
            resp = await call_tool(client, "get_jupyter_server", {"name": "nope"})
        assert result_of(resp)["isError"] is True
        (line,) = [x for x in parsed(log_lines) if x["event"] == "tool.call"]
        assert line["outcome"] == "error"

    async def test_never_logs_the_token_or_arguments(
        self, settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
    ) -> None:
        async with http_client(settings, fake_hub, log_lines) as client:
            await call_tool(client, "get_jupyter_server", {"name": "secret-name"})
        assert ALICE_TOKEN not in log_lines.getvalue()
        assert "secret-name" not in log_lines.getvalue()


class TestProbeNoise:
    async def test_successful_healthz_writes_no_access_line(
        self, settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
    ) -> None:
        async with http_client(settings, fake_hub, log_lines) as client:
            assert (await client.get("/healthz")).status_code == 200
        assert not [x for x in parsed(log_lines) if x.get("path") == "/healthz"]

    async def test_failing_probe_is_logged(self, log_lines: io.StringIO) -> None:
        async def failing(_request: Any) -> PlainTextResponse:
            return PlainTextResponse("down", status_code=503)

        app = Starlette(routes=[Route("/healthz", failing)])
        app.add_middleware(RequestLogMiddleware)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=RESOURCE_URL
        ) as client:
            await client.get("/healthz")
        (line,) = [x for x in parsed(log_lines) if x["logger"] != "httpx2"]
        assert (line["path"], line["status"]) == ("/healthz", 503)

    async def test_other_requests_keep_their_access_line(
        self, settings: Settings, fake_hub: FakeHub, log_lines: io.StringIO
    ) -> None:
        async with http_client(settings, fake_hub, log_lines) as client:
            resp = await client.post("/mcp", json={})
        (line,) = [x for x in parsed(log_lines) if x["event"] == "http.request"]
        assert (line["path"], line["status"]) == ("/mcp", resp.status_code)
