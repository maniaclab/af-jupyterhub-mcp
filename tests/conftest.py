"""Shared pytest fixtures for af-jupyterhub-mcp tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import TextContent

from af_jupyterhub_mcp.auth.hub import HubTokenVerifier
from af_jupyterhub_mcp.config import Settings
from af_jupyterhub_mcp.hub.client import HubClient
from tests.fakes import ALICE_TOKEN, HUB_URL, FakeHub, make_notebook_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from mcp.types import CallToolResult

CPU_IMAGE = "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest"
GPU_IMAGE = "hub.opensciencegrid.org/usatlas/ml-platform-gpu:latest"
PUBLIC_HUB_URL = "https://jupyterhub.example.org"


@pytest.fixture
def tool_text() -> Callable[[CallToolResult], str]:
    """Return a helper that extracts a tool's CallToolResult's markdown text block.

    Every tool in this repo returns exactly one TextContent block alongside
    its (optional) structured_content -- this is the substring-assertion
    equivalent of the plain-string return every tool used to have.
    """

    def _tool_text(result: CallToolResult) -> str:
        block = result.content[0]
        assert isinstance(block, TextContent)
        return block.text

    return _tool_text


@pytest.fixture
def fake_hub() -> FakeHub:
    """An empty in-memory hub whose proxy serves the fake jupyter-mcp-server."""
    return FakeHub(notebook_app=make_notebook_app)


@pytest.fixture
async def hub(fake_hub: FakeHub) -> AsyncIterator[HubClient]:
    """A HubClient wired to ``fake_hub``."""
    client = HubClient(HUB_URL, transport=fake_hub.transport())
    yield client
    await client.aclose()


@pytest.fixture
def settings() -> Settings:
    """Settings pointing at the fake hub, with links on a distinct public URL."""
    return Settings(
        hub_url=HUB_URL,
        public_hub_url=PUBLIC_HUB_URL,
        cpu_images=(CPU_IMAGE,),
        gpu_images=(GPU_IMAGE,),
        sites=("local", "ssl"),
        launch_timeout_seconds=5,
    )


@pytest.fixture
def make_ctx(hub: HubClient, settings: Settings) -> Callable[..., MagicMock]:
    """Return a factory for a tool Context carrying a bearer and the lifespan state.

    ``report_progress`` is an AsyncMock so tests can assert on the progress
    notifications a tool sent.
    """

    def _make_ctx(
        *, token: str = ALICE_TOKEN, settings_override: Settings | None = None
    ) -> MagicMock:
        ctx = MagicMock()
        ctx.request_context.request.headers = {"authorization": f"Bearer {token}"}
        lifespan: dict[str, Any] = {
            "settings": settings_override or settings,
            "hub": hub,
            "verifier": HubTokenVerifier(hub, ttl_seconds=60),
        }
        ctx.request_context.lifespan_context = lifespan
        ctx.report_progress = AsyncMock()
        return ctx

    return _make_ctx
