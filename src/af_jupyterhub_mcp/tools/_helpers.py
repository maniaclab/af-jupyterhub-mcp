"""Shared helpers for af-jupyterhub-mcp tool implementations."""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, TextContent


def format_error(
    exc: Exception,
    context: str = "",
    hints: list[str] | None = None,
) -> CallToolResult:
    """Format *exc* as an LLM-facing ``is_error`` result, never raising.

    Mirrors ami-mcp's ``tools._helpers.format_error`` -- errors are always
    returned as an ``is_error`` result, never raised as a bare exception to
    the LLM. No ``structured_content`` is set: an error result carries no
    structured payload (mcp SDK's ``convert_result`` only validates
    ``structured_content`` against the tool's output model when ``is_error``
    is false).
    """
    lines = [f"**Error**: {exc}"]
    if context:
        lines.append(f"\n{context}")
    if hints:
        lines.append("\n**Try:**")
        lines.extend(f"- {h}" for h in hints)
    text = "\n".join(lines)
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def append_next_actions(output: str, hints: list[str]) -> str:
    """Append a '## Next steps' section to tool output."""
    if not hints:
        return output
    hint_lines = "\n".join(f"- {h}" for h in hints)
    return f"{output}\n\n---\n**Next steps:**\n{hint_lines}"


_SUMMARY_FIELDS = (
    "name",
    "status",
    "image",
    "site",
    "cpu",
    "memory_gb",
    "qos",
    "gpus",
    "gpu_product",
    "binder_request",
    "url",
    "started",
    "last_activity",
)


def format_server(info: dict[str, Any]) -> str:
    """Format one server summary as an LLM-friendly Field | Value table.

    Fields that are unset (``None``) are left out rather than shown empty.
    """
    lines = ["| Field | Value |", "| --- | --- |"]
    lines.extend(
        f"| {key} | {info[key]} |"
        for key in _SUMMARY_FIELDS
        if info.get(key) is not None
    )
    return "\n".join(lines)


def format_server_list(infos: list[dict[str, Any]]) -> str:
    """Format a list of server summaries as an LLM-friendly markdown table."""
    if not infos:
        return "No servers found."
    keys = ["name", "status", "image", "site", "url"]
    header = "| " + " | ".join(keys) + " |"
    separator = "| " + " | ".join("---" for _ in keys) + " |"
    lines = [header, separator]
    lines.extend(
        "| "
        + " | ".join("" if info.get(k) is None else str(info[k]) for k in keys)
        + " |"
        for info in infos
    )
    return "\n".join(lines)
