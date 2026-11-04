"""Shared pytest fixtures for af-jupyterhub-mcp tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from mcp.types import TextContent

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.types import CallToolResult


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
