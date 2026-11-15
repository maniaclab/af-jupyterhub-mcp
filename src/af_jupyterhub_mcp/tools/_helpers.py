"""Shared helpers for af-jupyterhub-mcp tool implementations."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Sequence


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


# Raster types MCP clients render from ImageContent (the set LLM vision
# inputs accept). SVG is XML text, so it is returned as text instead.
_RASTER_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_SVG_MIME_TYPE = "image/svg+xml"
_OMITTED_HINT = (
    "the figure is still saved in the notebook; re-render it smaller "
    "(lower dpi or figsize) or open the notebook in JupyterLab"
)


class ImageOutput(BaseModel):
    """Metadata for one image output of a notebook tool call.

    The pixels travel in the result's image content blocks, never here.
    ``delivery`` says how the caller received it: as an MCP ``image`` block,
    inline as ``text`` (SVG), or ``omitted`` (over a size cap, or a type
    clients cannot render).
    """

    index: int
    mime_type: str
    size_bytes: int
    delivery: Literal["image", "text", "omitted"]


@dataclass(frozen=True)
class RenderedOutputs:
    """Notebook outputs ready to return: content blocks, plain text, image metadata."""

    content: list[ContentBlock]
    text: str
    images: list[ImageOutput]


def _format_size(size: int) -> str:
    """Return *size* bytes as ``70 B``, ``4.1 kB`` or ``1.5 MB`` (decimal units)."""
    if size < 1000:
        return f"{size} B"
    if size < 1_000_000:
        return f"{size / 1000:.1f} kB"
    return f"{size / 1_000_000:.1f} MB"


def _decoded_size(b64: str) -> int:
    """Return the byte length of base64 *b64* without decoding it."""
    return len(b64) * 3 // 4 - b64.count("=", -2)


def _render_image(
    block: ImageContent,
    index: int,
    *,
    forwarded_bytes: int,
    max_image_bytes: int,
    max_total_bytes: int,
) -> tuple[str, ImageContent | None, ImageOutput]:
    """Decide how one image output is returned: ``(text, image block, metadata)``.

    The text always starts with the ``[<mime> output <n>, <size>]`` marker so
    a client that does not render images still knows a figure exists.
    """
    mime = block.mime_type
    size = _decoded_size(block.data)
    marker = f"[{mime} output {index}, {_format_size(size)}"

    def omitted(reason: str) -> tuple[str, None, ImageOutput]:
        text = f"{marker} omitted: {reason}]"
        return (
            text,
            None,
            ImageOutput(
                index=index, mime_type=mime, size_bytes=size, delivery="omitted"
            ),
        )

    if mime not in _RASTER_MIME_TYPES and mime != _SVG_MIME_TYPE:
        return omitted(f"{mime} is not a type MCP clients render")
    if size > max_image_bytes:
        return omitted(
            f"over the per-image limit of {_format_size(max_image_bytes)}; "
            f"{_OMITTED_HINT}"
        )
    if forwarded_bytes + size > max_total_bytes:
        return omitted(
            f"over the total limit of {_format_size(max_total_bytes)} of images "
            f"per call; {_OMITTED_HINT}"
        )
    if mime == _SVG_MIME_TYPE:
        try:
            svg = base64.b64decode(block.data, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return omitted("the SVG data could not be decoded")
        info = ImageOutput(
            index=index, mime_type=mime, size_bytes=size, delivery="text"
        )
        return f"{marker}]\n{svg}", None, info
    info = ImageOutput(index=index, mime_type=mime, size_bytes=size, delivery="image")
    return f"{marker}]", block, info


def render_outputs(
    blocks: Sequence[ContentBlock],
    *,
    max_image_bytes: int,
    max_total_bytes: int,
) -> RenderedOutputs:
    """Turn a notebook tool's content blocks into the result returned to the caller.

    Text blocks pass through; each image block gets a text marker (in order,
    among the text) and is forwarded as MCP image content unless it is over
    *max_image_bytes*, would take the call's forwarded images past
    *max_total_bytes*, or is a type clients cannot render -- those are marked
    as omitted, never dropped silently. Text-only output is one text block,
    joined with newlines. Other block types (resources, ...) are ignored.

    The text parts are also returned joined as ``text``, for the structured
    result: the same information as the text blocks, without image data.
    """
    parts: list[str] = []
    content: list[ContentBlock] = []
    images: list[ImageOutput] = []
    forwarded_bytes = 0

    def flush_text() -> None:
        if parts:
            content.append(TextContent(type="text", text="\n".join(parts)))
            parts.clear()

    full_text: list[str] = []
    for block in blocks:
        if isinstance(block, TextContent):
            parts.append(block.text)
            full_text.append(block.text)
        elif isinstance(block, ImageContent):
            text, image, info = _render_image(
                block,
                len(images) + 1,
                forwarded_bytes=forwarded_bytes,
                max_image_bytes=max_image_bytes,
                max_total_bytes=max_total_bytes,
            )
            images.append(info)
            parts.append(text)
            full_text.append(text)
            if image is not None:
                forwarded_bytes += info.size_bytes
                flush_text()
                content.append(image)
    flush_text()
    if not content:
        content.append(TextContent(type="text", text="(no output)"))
        full_text.append("(no output)")
    return RenderedOutputs(content=content, text="\n".join(full_text), images=images)
