"""End-to-end tests against the live AF JupyterHub. No mocks, no fakes.

Skipped unless ``AF_JUPYTERHUB_E2E_TOKEN`` holds a real hub token with the
required scopes (``servers!user``, ``access:servers!user``,
``read:users:name!user``), e.g. one minted at ``/hub/token``. Run with::

    AF_JUPYTERHUB_E2E_TOKEN=... pixi run -e py312 test-e2e

Optional:

- ``AF_JUPYTERHUB_E2E_IMAGE``: image to launch (default
  ``hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest``); it must ship
  jupyterhub-singleuser and jupyter-mcp-server.
- ``JUPYTERHUB_MCP_HUB_URL``: hub to test against (default the AF hub).

The test starts a named server ``mcp-e2e`` for the token's owner, runs code
in it through the hub proxy, and deletes it again, even on failure.
"""

from __future__ import annotations

import dataclasses
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from af_jupyterhub_mcp.config import Settings
from af_jupyterhub_mcp.server import make_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_TOKEN = os.environ.get("AF_JUPYTERHUB_E2E_TOKEN", "")
_IMAGE = os.environ.get(
    "AF_JUPYTERHUB_E2E_IMAGE",
    "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest",
)
_SERVER = "mcp-e2e"
_RESOURCE_URL = "http://127.0.0.1:8000"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _TOKEN, reason="AF_JUPYTERHUB_E2E_TOKEN is not set"),
]


@asynccontextmanager
async def live_session() -> AsyncIterator[ClientSession]:
    """Run the real app against the live hub and yield an initialized session."""
    settings = dataclasses.replace(Settings.from_env(), cpu_images=(_IMAGE,))
    app = make_app(settings=settings, resource_url=_RESOURCE_URL, host="127.0.0.1")
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=_RESOURCE_URL,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=httpx2.Timeout(30.0, read=settings.launch_timeout_seconds + 60),
        ) as http_client,
        streamable_http_client(f"{_RESOURCE_URL}/mcp", http_client=http_client) as (
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
    return str(block.text)


class TestLiveAfJupyterHub:
    async def test_launch_list_execute_delete(self) -> None:
        async with live_session() as session:
            # Start from a clean slate if a previous run was interrupted.
            await session.call_tool("delete_jupyter_server", {"name": _SERVER})
            try:
                created = await session.call_tool(
                    "create_jupyter_server", {"image": _IMAGE, "name": _SERVER}
                )
                assert created.is_error is not True, _text(created)
                server = (created.structured_content or {})["server"]
                assert server["status"] == "ready"
                assert server["image"] == _IMAGE

                listed = await session.call_tool(
                    "nb_list_files", {"server_name": _SERVER}
                )
                assert listed.is_error is not True, _text(listed)

                executed = await session.call_tool(
                    "nb_execute_code",
                    {"server_name": _SERVER, "code": "print(1+1)"},
                )
                assert executed.is_error is not True, _text(executed)
                assert "2" in _text(executed)
            finally:
                deleted = await session.call_tool(
                    "delete_jupyter_server", {"name": _SERVER}
                )
            assert deleted.is_error is not True, _text(deleted)
            for result in (created, listed, executed, deleted):
                assert _TOKEN not in _text(result)
