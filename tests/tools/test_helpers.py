"""Unit tests for af_jupyterhub_mcp.tools._helpers."""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from af_jupyterhub_mcp.tools._helpers import (
    append_next_actions,
    format_error,
    format_server,
    format_server_list,
)


def _error_text(result: CallToolResult) -> str:
    """Extract the sole text block's content from an is_error CallToolResult."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


class TestFormatError:
    def test_returns_an_is_error_call_tool_result(self) -> None:
        result = format_error(ValueError("bad"))
        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        assert result.structured_content is None

    def test_message_is_included(self) -> None:
        result = format_error(ValueError("bad thing happened"))
        assert "bad thing happened" in _error_text(result)

    def test_context_is_included(self) -> None:
        result = format_error(ValueError("bad"), context="extra context")
        assert "extra context" in _error_text(result)

    def test_hints_are_appended(self) -> None:
        result = format_error(ValueError("bad"), hints=["Try again."])
        assert "Try again." in _error_text(result)
        assert "**Try:**" in _error_text(result)

    def test_no_hints_omits_try_section(self) -> None:
        result = format_error(ValueError("bad"))
        assert "**Try:**" not in _error_text(result)


def test_append_next_actions_appends_bulleted_list() -> None:
    output = append_next_actions("base output", ["Do X.", "Do Y."])
    assert "base output" in output
    assert "- Do X." in output
    assert "- Do Y." in output


def test_append_next_actions_no_hints_returns_unchanged() -> None:
    assert append_next_actions("base output", []) == "base output"


class TestFormatServer:
    def test_only_set_known_fields_are_shown(self) -> None:
        table = format_server(
            {"name": "nb-1", "status": "ready", "gpu_product": None, "other": "x"}
        )
        assert "| name | nb-1 |" in table
        assert "| status | ready |" in table
        assert "gpu_product" not in table
        assert "other" not in table


class TestFormatServerList:
    def test_empty_list_says_no_servers_found(self) -> None:
        assert format_server_list([]) == "No servers found."

    def test_lists_summary_columns(self) -> None:
        table = format_server_list(
            [{"name": "nb-1", "status": "ready", "image": "img", "site": None}]
        )
        assert table.splitlines()[0] == "| name | status | image | site | url |"
        assert "| nb-1 | ready | img |  |  |" in table
