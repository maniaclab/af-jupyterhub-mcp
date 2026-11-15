"""Unit tests for af_jupyterhub_mcp.tools._helpers."""

from __future__ import annotations

import base64

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from af_jupyterhub_mcp.tools._helpers import (
    ImageOutput,
    append_next_actions,
    format_error,
    format_server,
    format_server_list,
    render_outputs,
)
from tests.fakes import PNG_1X1


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


def _png(data: bytes = PNG_1X1) -> ImageContent:
    return ImageContent(
        type="image", data=base64.b64encode(data).decode(), mime_type="image/png"
    )


def _text(text: str) -> TextContent:
    return TextContent(type="text", text=text)


_BIG = 10_000_000
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'


class TestRenderOutputs:
    def test_text_only_is_one_unchanged_text_block(self) -> None:
        rendered = render_outputs(
            [_text("a\n"), _text("b")], max_image_bytes=_BIG, max_total_bytes=_BIG
        )
        assert rendered.content == [_text("a\n\nb")]
        assert rendered.text == "a\n\nb"
        assert rendered.images == []

    def test_no_blocks_says_so(self) -> None:
        rendered = render_outputs([], max_image_bytes=_BIG, max_total_bytes=_BIG)
        assert rendered.text == "(no output)"
        assert rendered.content == [_text("(no output)")]

    def test_png_is_forwarded_as_is_after_a_marker_in_order(self) -> None:
        image = _png()
        rendered = render_outputs(
            [_text("before"), image, _text("after-plot")],
            max_image_bytes=_BIG,
            max_total_bytes=_BIG,
        )
        marker = f"[image/png output 1, {len(PNG_1X1)} B]"
        assert rendered.content == [
            _text(f"before\n{marker}"),
            image,
            _text("after-plot"),
        ]
        assert rendered.text == f"before\n{marker}\nafter-plot"
        assert rendered.images == [
            ImageOutput(
                index=1,
                mime_type="image/png",
                size_bytes=len(PNG_1X1),
                delivery="image",
            )
        ]

    def test_image_only_output_still_has_a_marker(self) -> None:
        rendered = render_outputs([_png()], max_image_bytes=_BIG, max_total_bytes=_BIG)
        assert isinstance(rendered.content[0], TextContent)
        assert "[image/png output 1" in rendered.content[0].text
        assert isinstance(rendered.content[1], ImageContent)

    @pytest.mark.parametrize("mime", ["image/jpeg", "image/gif", "image/webp"])
    def test_other_raster_types_are_forwarded(self, mime: str) -> None:
        image = ImageContent(type="image", data=_png().data, mime_type=mime)
        rendered = render_outputs([image], max_image_bytes=_BIG, max_total_bytes=_BIG)
        assert rendered.content[1] == image
        assert rendered.images[0].mime_type == mime
        assert rendered.images[0].delivery == "image"

    def test_images_are_numbered_in_order(self) -> None:
        rendered = render_outputs(
            [_png(), _text("x"), _png()], max_image_bytes=_BIG, max_total_bytes=_BIG
        )
        assert [i.index for i in rendered.images] == [1, 2]
        assert "output 2" in rendered.text

    def test_svg_is_returned_as_text_not_image_content(self) -> None:
        svg = ImageContent(
            type="image",
            data=base64.b64encode(_SVG).decode(),
            mime_type="image/svg+xml",
        )
        rendered = render_outputs([svg], max_image_bytes=_BIG, max_total_bytes=_BIG)
        assert not any(isinstance(c, ImageContent) for c in rendered.content)
        assert "[image/svg+xml output 1" in rendered.text
        assert _SVG.decode() in rendered.text
        assert rendered.images[0].delivery == "text"

    def test_oversized_svg_is_omitted_with_a_note(self) -> None:
        svg = ImageContent(
            type="image",
            data=base64.b64encode(_SVG).decode(),
            mime_type="image/svg+xml",
        )
        rendered = render_outputs([svg], max_image_bytes=10, max_total_bytes=_BIG)
        assert _SVG.decode() not in rendered.text
        assert "omitted" in rendered.text
        assert rendered.images[0].delivery == "omitted"

    def test_unsupported_mime_is_omitted_with_a_note(self) -> None:
        tiff = ImageContent(type="image", data=_png().data, mime_type="image/tiff")
        rendered = render_outputs([tiff], max_image_bytes=_BIG, max_total_bytes=_BIG)
        assert not any(isinstance(c, ImageContent) for c in rendered.content)
        assert "[image/tiff output 1" in rendered.text
        assert "omitted" in rendered.text
        assert "image/tiff" in rendered.text
        assert rendered.images[0].delivery == "omitted"

    def test_image_over_the_per_image_cap_is_omitted_with_a_note(self) -> None:
        big = _png(PNG_1X1 + b"\0" * 5_000)
        rendered = render_outputs(
            [big, _text("tail")], max_image_bytes=1_000, max_total_bytes=_BIG
        )
        assert not any(isinstance(c, ImageContent) for c in rendered.content)
        assert "[image/png output 1, 5.1 kB omitted" in rendered.text
        assert "per-image limit of 1.0 kB" in rendered.text
        assert "saved in the notebook" in rendered.text
        assert rendered.text.endswith("tail")
        assert rendered.images[0].delivery == "omitted"
        assert rendered.images[0].size_bytes == len(PNG_1X1) + 5_000

    def test_total_cap_omits_the_image_that_would_cross_it(self) -> None:
        one = len(PNG_1X1)
        rendered = render_outputs(
            [_png(), _png(), _png()],
            max_image_bytes=_BIG,
            max_total_bytes=2 * one,
        )
        assert [i.delivery for i in rendered.images] == ["image", "image", "omitted"]
        assert "total limit" in rendered.text
        assert sum(isinstance(c, ImageContent) for c in rendered.content) == 2

    def test_undecodable_svg_is_omitted_not_raised(self) -> None:
        bad = ImageContent(
            type="image", data="!!not base64!!", mime_type="image/svg+xml"
        )
        rendered = render_outputs([bad], max_image_bytes=_BIG, max_total_bytes=_BIG)
        assert rendered.images[0].delivery == "omitted"
        assert "omitted" in rendered.text
