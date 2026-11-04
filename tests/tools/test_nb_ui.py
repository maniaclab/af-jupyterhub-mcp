"""Tests for the nb_ui_* proxy tools in nb_ui.py.

These proxy jupyter-mcp-tools JupyterLab frontend commands. The expected
command ids and read-only/mutating/destructive buckets are spelled out here
independently of ``nb_ui._UI_TOOLS`` so a typo or a mis-bucketed row in the
implementation's table fails a test instead of silently agreeing with itself.

call_notebook_tool is patched for the forwarding tests; one test goes
through the fake hub proxy to show a UI call takes the same path as nb_*.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.mcpserver import MCPServer

from af_jupyterhub_mcp.tools import nb_proxy as nb_proxy_mod
from af_jupyterhub_mcp.tools import nb_ui as nb_ui_mod
from tests.conftest import CPU_IMAGE

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from unittest.mock import MagicMock

    from mcp.server.mcpserver.tools import Tool
    from mcp.types import CallToolResult, ToolAnnotations

    from af_jupyterhub_mcp.config import Settings
    from tests.fakes import FakeHub

_PATCH = "af_jupyterhub_mcp.tools.nb_proxy.call_notebook_tool"

_READ_ONLY: set[str] = {
    "notebook_get-selected-cell",
    "notebook_copy-cell",
    "notebook_move-cursor-down",
    "notebook_move-cursor-up",
    "notebook_extend-marked-cells-below",
    "notebook_extend-marked-cells-above",
    "filebrowser_go-to-path",
    "filebrowser_refresh",
    "filebrowser_toggle-hidden-files",
    "application_toggle-left-area",
    "application_toggle-right-area",
    "application_toggle-presentation-mode",
    "apputils_change-theme",
    "editmenu_open",
    "filemenu_open",
    "helpmenu_open",
    "documentsearch_start",
    "documentsearch_highlightNext",
    "documentsearch_highlightPrevious",
}
_MUTATING: set[str] = {
    "notebook_insert-cell-below",
    "notebook_insert-cell-above",
    "notebook_paste-cell-below",
    "notebook_paste-cell-above",
    "notebook_move-cell-up",
    "notebook_move-cell-down",
    "notebook_split-cell-at-cursor",
    "notebook_change-cell-to-code",
    "notebook_change-cell-to-markdown",
    "notebook_change-cell-to-raw",
    "console_create",
    "docmanager_open",
    "docmanager_new-untitled",
    "docmanager_save",
    "docmanager_duplicate",
    "filebrowser_create-new-directory",
    "kernelmenu_reconnect-to-kernel",
    "terminal_create-new",
    "terminal_refresh",
}
_DESTRUCTIVE: set[str] = {
    "notebook_delete-cell",
    "notebook_cut-cell",
    "notebook_merge-cell-above",
    "notebook_merge-cell-below",
    "notebook_run-all-cells",
    "notebook_run-cell",
    "notebook_run-cell-and-select-next",
    "notebook_run-cell-and-insert-below",
    "notebook_append-execute",
    "console_clear",
    "console_interrupt-kernel",
    "console_inject",
    "kernelmenu_interrupt",
    "kernelmenu_shutdown",
}
_ALL_UPSTREAM = _READ_ONLY | _MUTATING | _DESTRUCTIVE

# Upstream commands that take arguments, so get hand-written typed wrappers.
_TYPED = {
    "notebook_append-execute",
    "console_create",
    "console_inject",
    "docmanager_open",
    "docmanager_new-untitled",
    "filebrowser_go-to-path",
    "apputils_change-theme",
    "documentsearch_start",
}
_ARG_FREE = sorted(_ALL_UPSTREAM - _TYPED)

# Commands deliberately not proxied: they act on the human's browser or block
# on a modal dialog that needs a human click.
_EXCLUDED = {
    "filebrowser_upload",
    "filebrowser_download",
    "docmanager_delete",
    "docmanager_rename",
    "docmanager_save-as",
    "kernelmenu_change",
    "kernelmenu_restart",
    "console_restart-kernel",
}


def _proxy_name(upstream: str) -> str:
    return "nb_ui_" + upstream.replace("-", "_")


def _registered() -> list[Tool]:
    mcp = MCPServer("test")
    nb_ui_mod.register(mcp)
    return mcp._tool_manager.list_tools()


@pytest.fixture
def ready_ctx(fake_hub: FakeHub, make_ctx: Callable[..., MagicMock]) -> MagicMock:
    """A context for alice, who has a ready server nb-1 on the CPU image."""
    fake_hub.add_server("alice", "nb-1", user_options={"image": CPU_IMAGE})
    return make_ctx()


@pytest.fixture
def registered_ui_tools() -> dict[str, Callable[..., Awaitable[CallToolResult]]]:
    return {tool.name: tool.fn for tool in _registered()}


# ---------------------------------------------------------------------------
# Tests: registration, naming, schemas, annotations
# ---------------------------------------------------------------------------


class TestNbUiRegistration:
    def test_expected_buckets_are_disjoint_and_total_52(self) -> None:
        assert not (_READ_ONLY & _MUTATING)
        assert not (_READ_ONLY & _DESTRUCTIVE)
        assert not (_MUTATING & _DESTRUCTIVE)
        assert len(_ALL_UPSTREAM) == 52

    def test_registers_exactly_the_expected_tools(self) -> None:
        names = {t.name for t in _registered()}
        assert names == {_proxy_name(u) for u in _ALL_UPSTREAM}

    def test_excluded_commands_are_not_registered(self) -> None:
        names = {t.name for t in _registered()}
        assert not names & {_proxy_name(u) for u in _EXCLUDED}

    def test_no_name_collides_with_nb_proxy_tools(self) -> None:
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        proxy_names = {t.name for t in mcp._tool_manager.list_tools()}
        ui_names = {t.name for t in _registered()}
        assert not proxy_names & ui_names

    def test_server_name_required_and_ctx_hidden(self) -> None:
        for tool in _registered():
            params = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            assert "server_name" in required, tool.name
            assert "ctx" not in params, tool.name

    def test_arg_free_tools_take_only_server_name(self) -> None:
        by_name = {t.name: t for t in _registered()}
        for upstream in _ARG_FREE:
            tool = by_name[_proxy_name(upstream)]
            assert set(tool.parameters["properties"]) == {"server_name"}, tool.name

    def test_every_description_warns_about_the_open_tab(self) -> None:
        for tool in _registered():
            assert tool.description.startswith(nb_ui_mod.OPEN_TAB_NOTE), tool.name

    def test_every_tool_declares_title_and_output_schema(self) -> None:
        for tool in _registered():
            assert tool.annotations is not None, tool.name
            assert tool.annotations.title, tool.name
            assert tool.output_schema is not None, tool.name


def _annotations(upstream: str) -> ToolAnnotations:
    by_name = {t.name: t for t in _registered()}
    annotations = by_name[_proxy_name(upstream)].annotations
    assert annotations is not None, upstream
    return annotations


class TestNbUiAnnotations:
    @pytest.mark.parametrize("upstream", sorted(_READ_ONLY))
    def test_read_only(self, upstream: str) -> None:
        ann = _annotations(upstream)
        assert ann.read_only_hint is True
        assert ann.open_world_hint is True

    @pytest.mark.parametrize("upstream", sorted(_MUTATING))
    def test_mutating(self, upstream: str) -> None:
        ann = _annotations(upstream)
        assert ann.read_only_hint is False
        assert ann.destructive_hint is None

    @pytest.mark.parametrize("upstream", sorted(_DESTRUCTIVE))
    def test_destructive(self, upstream: str) -> None:
        ann = _annotations(upstream)
        assert ann.read_only_hint is False
        assert ann.destructive_hint is True


# ---------------------------------------------------------------------------
# Tests: forwarding to upstream
# ---------------------------------------------------------------------------


class TestNbUiForwarding:
    @pytest.mark.parametrize("upstream", _ARG_FREE)
    async def test_arg_free_tool_forwards_exact_upstream_id(
        self,
        registered_ui_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        ready_ctx: MagicMock,
        upstream: str,
    ) -> None:
        ctx = ready_ctx
        with patch(
            _PATCH,
            new=AsyncMock(return_value="ok"),
        ) as mock_call:
            result = await registered_ui_tools[_proxy_name(upstream)](
                server_name="nb-1", ctx=ctx
            )

        assert result.is_error is not True
        assert result.structured_content == {"result": "ok"}
        kwargs = mock_call.call_args.kwargs
        assert kwargs["tool_name"] == upstream
        assert kwargs["tool_args"] == {}

    @pytest.mark.parametrize(
        ("upstream", "call_args", "expected_args"),
        [
            pytest.param(
                "notebook_append-execute",
                {"source": "print(1)"},
                {"source": "print(1)", "type": "code"},
                id="append-execute-defaults",
            ),
            pytest.param(
                "notebook_append-execute",
                {"source": "# Title", "cell_type": "markdown"},
                {"source": "# Title", "type": "markdown"},
                id="append-execute-markdown",
            ),
            pytest.param(
                "console_create",
                {},
                {"activate": True, "insertMode": "split-right"},
                id="console-create-defaults",
            ),
            pytest.param(
                "console_create",
                {"path": "work", "insert_mode": "split-bottom", "activate": False},
                {"activate": False, "insertMode": "split-bottom", "path": "work"},
                id="console-create-all",
            ),
            pytest.param(
                "console_inject",
                {"code": "x = 1"},
                {"code": "x = 1", "activate": True},
                id="console-inject-defaults",
            ),
            pytest.param(
                "console_inject",
                {"code": "x = 1", "path": "work/Console 1", "activate": False},
                {"code": "x = 1", "activate": False, "path": "work/Console 1"},
                id="console-inject-all",
            ),
            pytest.param(
                "docmanager_open",
                {"path": "analysis.ipynb"},
                {"path": "analysis.ipynb"},
                id="docmanager-open",
            ),
            pytest.param(
                "docmanager_open",
                {"path": "data.csv", "factory": "Editor"},
                {"path": "data.csv", "factory": "Editor"},
                id="docmanager-open-factory",
            ),
            pytest.param(
                "docmanager_new-untitled",
                {"content_type": "notebook"},
                {"type": "notebook", "path": ""},
                id="new-untitled-notebook",
            ),
            pytest.param(
                "docmanager_new-untitled",
                {"content_type": "file", "path": "work", "ext": ".py"},
                {"type": "file", "path": "work", "ext": ".py"},
                id="new-untitled-file",
            ),
            pytest.param(
                "filebrowser_go-to-path",
                {"path": "work/data"},
                {"path": "work/data"},
                id="go-to-path",
            ),
            pytest.param(
                "apputils_change-theme",
                {"theme": "JupyterLab Dark"},
                {"theme": "JupyterLab Dark"},
                id="change-theme",
            ),
            pytest.param(
                "documentsearch_start",
                {},
                {},
                id="search-start-empty",
            ),
            pytest.param(
                "documentsearch_start",
                {"search_text": "TODO"},
                {"searchText": "TODO"},
                id="search-start-text",
            ),
        ],
    )
    async def test_typed_tool_maps_args(
        self,
        registered_ui_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        ready_ctx: MagicMock,
        upstream: str,
        call_args: dict[str, object],
        expected_args: dict[str, object],
    ) -> None:
        ctx = ready_ctx
        with patch(
            _PATCH,
            new=AsyncMock(return_value="ok"),
        ) as mock_call:
            result = await registered_ui_tools[_proxy_name(upstream)](
                server_name="nb-1", **call_args, ctx=ctx
            )

        assert result.is_error is not True
        kwargs = mock_call.call_args.kwargs
        assert kwargs["tool_name"] == upstream
        assert kwargs["tool_args"] == expected_args

    async def test_not_your_notebook_is_a_formatted_error(
        self,
        registered_ui_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = make_ctx()
        with patch(
            _PATCH,
            new=AsyncMock(return_value="ok"),
        ) as mock_call:
            result = await registered_ui_tools["nb_ui_notebook_run_all_cells"](
                server_name="bobs-server", ctx=ctx
            )

        mock_call.assert_not_called()
        assert result.is_error is True
        assert "list_jupyter_servers" in tool_text(result)


class TestNbUiImageToolOverrides:
    """An image running jupyter-mcp-server 1.x only offers two frontend tools.

    The image is the server's ``user_options.image``.
    """

    _ONE_X_UI = frozenset({"notebook_run-all-cells", "notebook_get-selected-cell"})

    async def test_override_allows_listed_and_blocks_other_ui_tools(
        self,
        registered_ui_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        fake_hub: FakeHub,
        make_ctx: Callable[..., MagicMock],
    ) -> None:
        fake_hub.add_server("alice", "nb-1", user_options={"image": CPU_IMAGE})
        ctx = make_ctx(
            settings_override=dataclasses.replace(
                settings, image_tool_overrides={CPU_IMAGE: self._ONE_X_UI}
            )
        )
        with patch(
            _PATCH,
            new=AsyncMock(return_value="ok"),
        ) as mock_call:
            allowed = await registered_ui_tools["nb_ui_notebook_run_all_cells"](
                server_name="nb-1", ctx=ctx
            )
            blocked = await registered_ui_tools["nb_ui_docmanager_open"](
                server_name="nb-1", path="a.ipynb", ctx=ctx
            )

        assert allowed.is_error is not True
        assert blocked.is_error is True
        mock_call.assert_called_once()
        assert mock_call.call_args.kwargs["tool_name"] == "notebook_run-all-cells"


class TestNbUiThroughTheHubProxy:
    async def test_ui_call_reaches_the_notebook_endpoint(
        self,
        registered_ui_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        ready_ctx: MagicMock,
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await registered_ui_tools["nb_ui_notebook_run_all_cells"](
            server_name="nb-1", ctx=ready_ctx
        )
        # The stand-in server has no frontend tools, so upstream reports an
        # unknown-tool error -- after the call went through the hub proxy.
        assert result.is_error is True
        assert "notebook_run-all-cells" in tool_text(result)
        assert any(r.url.path == "/user/alice/nb-1/mcp" for r in fake_hub.requests)
