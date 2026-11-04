"""Tests for the 17 nb_* proxy tools in nb_proxy.py.

These tests verify:
1. Each proxy tool only reaches the caller's own named servers.
2. Each proxy tool checks the server is ready before forwarding.
3. Calls reach jupyter-mcp-server through the hub proxy with the caller's
   hub token in a header, and the token is never returned.
4. Per-image tool overrides are enforced against ``user_options.image``.
5. ctx is injected by the SDK (not exposed as a user-facing arg).

The end-to-end path tests go through the fake hub's proxy to a real
mcp-SDK server; the argument-mapping tests patch ``call_notebook_tool``.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.mcpserver import MCPServer

from af_jupyterhub_mcp.hub.errors import NotebookNotReadyError, NotFoundOrNotYoursError
from af_jupyterhub_mcp.hub.proxy import (
    NotebookToolTransportError,
    NotebookToolUpstreamError,
)
from af_jupyterhub_mcp.tools import nb_proxy as nb_proxy_mod
from tests.conftest import CPU_IMAGE
from tests.fakes import ALICE_TOKEN, FakeHub

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from unittest.mock import MagicMock

    from mcp.types import CallToolResult

    from af_jupyterhub_mcp.config import Settings

    ToolFn = Callable[..., Awaitable[CallToolResult]]

_PATCH = "af_jupyterhub_mcp.tools.nb_proxy.call_notebook_tool"


@pytest.fixture
def registered_nb_tools() -> dict[str, ToolFn]:
    mcp = MCPServer("test")
    nb_proxy_mod.register(mcp)
    return {tool.name: tool.fn for tool in mcp._tool_manager.list_tools()}


@pytest.fixture
def ready_server(fake_hub: FakeHub) -> FakeHub:
    """Give alice a ready server nb-1 running the CPU image."""
    fake_hub.add_server("alice", "nb-1", user_options={"image": CPU_IMAGE})
    return fake_hub


# ---------------------------------------------------------------------------
# Tests: registration contract
# ---------------------------------------------------------------------------


class TestNbProxyContextInjection:
    def test_ctx_not_in_any_tool_parameters(self) -> None:
        """ctx must not appear in any nb_* tool's parameter schema."""
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            params = tool.parameters.get("properties", {})
            assert "ctx" not in params, (
                f"Tool '{tool.name}' exposes 'ctx' as a user-facing parameter."
            )

    def test_all_17_tools_are_registered(self) -> None:
        """Exactly 17 nb_* tools must be registered.

        The jupyter-mcp-tools frontend commands (get-selected-cell,
        run-all-cells, ...) live in nb_ui.py as nb_ui_* tools, not here.
        """
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        names = [t.name for t in mcp._tool_manager.list_tools()]
        assert len(names) == 17, f"Expected 17 tools, got {len(names)}: {names}"
        assert "nb_get_selected_cell" not in names
        assert "nb_run_all_cells" not in names

    def test_server_name_is_required_parameter(self) -> None:
        """server_name must be a required parameter of every nb_* tool."""
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            params = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            assert "server_name" in params, tool.name
            assert "server_name" in required, tool.name
            assert "notebook_server_id" not in params, tool.name


class TestNbProxyAnnotationsAndOutputSchema:
    """Drift guard, plus the read-only/mutating/destructive bucket assignment.

    See CLAUDE.md's "Tool registration pattern" for the rationale.
    """

    _READ_ONLY: ClassVar[set[str]] = {
        "nb_list_files",
        "nb_list_kernels",
        "nb_list_notebooks",
        "nb_read_notebook",
        "nb_read_cell",
    }
    _MUTATING: ClassVar[set[str]] = {
        "nb_use_notebook",
        "nb_unuse_notebook",
        "nb_insert_cell",
        "nb_edit_cell_source",
        "nb_move_cell",
    }
    _DESTRUCTIVE: ClassVar[set[str]] = {
        "nb_restart_notebook",
        "nb_overwrite_cell_source",
        "nb_delete_cell",
        "nb_execute_cell",
        "nb_insert_execute_code_cell",
        "nb_execute_code",
        "nb_clear_cell_output",
    }

    def test_buckets_cover_all_17_tools_with_no_overlap(self) -> None:
        all_buckets = self._READ_ONLY | self._MUTATING | self._DESTRUCTIVE
        assert len(all_buckets) == 17
        assert not (self._READ_ONLY & self._MUTATING)
        assert not (self._READ_ONLY & self._DESTRUCTIVE)
        assert not (self._MUTATING & self._DESTRUCTIVE)

    def test_every_tool_matches_its_bucket(self) -> None:
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            ann = tool.annotations
            assert ann is not None, tool.name
            assert tool.output_schema is not None, tool.name
            if tool.name in self._READ_ONLY:
                assert ann.read_only_hint is True, tool.name
            elif tool.name in self._MUTATING:
                assert ann.read_only_hint is False, tool.name
                assert ann.destructive_hint is None, tool.name
            else:
                assert tool.name in self._DESTRUCTIVE, tool.name
                assert ann.read_only_hint is False, tool.name
                assert ann.destructive_hint is True, tool.name


# ---------------------------------------------------------------------------
# Tests: the real path through the hub proxy
# ---------------------------------------------------------------------------


class TestThroughTheHubProxy:
    async def test_execute_code_reaches_the_notebook(
        self,
        registered_nb_tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        ready_server: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await registered_nb_tools["nb_execute_code"](
            server_name="nb-1", code="print(1+1)", timeout=60, ctx=make_ctx()
        )
        assert result.is_error is not True, tool_text(result)
        assert tool_text(result) == "ran 'print(1+1)' (timeout 60)"
        assert result.structured_content == {"result": tool_text(result)}
        proxied = [r for r in ready_server.requests if r.url.path.startswith("/user/")]
        assert {r.url.path for r in proxied} == {"/user/alice/nb-1/mcp"}
        assert all(
            r.headers["authorization"] == f"token {ALICE_TOKEN}" for r in proxied
        )

    async def test_list_files_reaches_the_notebook(
        self,
        registered_nb_tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        ready_server: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        assert ready_server.servers["alice"]["nb-1"]["ready"]
        result = await registered_nb_tools["nb_list_files"](
            server_name="nb-1", path="work", ctx=make_ctx()
        )
        assert tool_text(result) == "files in 'work' (depth 1)"

    async def test_token_never_in_result(
        self,
        registered_nb_tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        ready_server: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        assert ready_server.servers["alice"]
        result = await registered_nb_tools["nb_execute_code"](
            server_name="nb-1", code="x=1", ctx=make_ctx()
        )
        assert ALICE_TOKEN not in tool_text(result)
        assert ALICE_TOKEN not in json.dumps(result.structured_content)

    async def test_image_without_jupyter_mcp_server_is_a_transport_error(
        self,
        registered_nb_tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        ready_server: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ready_server.notebook_app = None  # the proxy answers 503
        result = await registered_nb_tools["nb_list_kernels"](
            server_name="nb-1", ctx=make_ctx()
        )
        assert result.is_error is True
        assert "get_jupyter_server" in tool_text(result)


# ---------------------------------------------------------------------------
# Tests: _get_ready_server raises typed errors
# ---------------------------------------------------------------------------


class TestGetReadyServer:
    async def test_unknown_server(self, make_ctx: Callable[..., MagicMock]) -> None:
        with pytest.raises(NotFoundOrNotYoursError):
            await nb_proxy_mod._get_ready_server(make_ctx(), "does-not-exist")

    async def test_another_users_server_is_not_found(
        self, make_ctx: Callable[..., MagicMock], fake_hub: FakeHub
    ) -> None:
        fake_hub.add_server("bob", "nb-1")
        with pytest.raises(NotFoundOrNotYoursError):
            await nb_proxy_mod._get_ready_server(make_ctx(), "nb-1")

    @pytest.mark.parametrize(
        ("ready", "pending", "status"),
        [(False, "spawn", "pending spawn"), (False, None, "stopped")],
    )
    async def test_not_ready(
        self,
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        ready: bool,
        pending: str | None,
        status: str,
    ) -> None:
        fake_hub.add_server("alice", "nb-1", ready=ready, pending=pending)
        with pytest.raises(NotebookNotReadyError, match=status):
            await nb_proxy_mod._get_ready_server(make_ctx(), "nb-1")

    async def test_returns_identity_and_model(
        self, make_ctx: Callable[..., MagicMock], ready_server: FakeHub
    ) -> None:
        identity, model = await nb_proxy_mod._get_ready_server(make_ctx(), "nb-1")
        assert identity.name == "alice"
        assert model == ready_server.servers["alice"]["nb-1"]


# ---------------------------------------------------------------------------
# Tests: _call_upstream formats each failure with the right hints
# ---------------------------------------------------------------------------


class TestCallUpstreamErrorFormatting:
    async def test_unknown_server_points_at_the_listing(
        self,
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        with patch(_PATCH) as mock_call:
            result = await nb_proxy_mod._call_upstream(make_ctx(), "nope", "x", {})
        mock_call.assert_not_called()
        assert result.is_error is True
        assert "list_jupyter_servers" in tool_text(result)

    async def test_bad_name_points_at_the_listing(
        self,
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await nb_proxy_mod._call_upstream(make_ctx(), "a/b", "x", {})
        assert result.is_error is True
        assert "list_jupyter_servers" in tool_text(result)

    async def test_pending_server_points_at_get(
        self,
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        fake_hub.add_server("alice", "nb-1", ready=False, pending="spawn")
        result = await nb_proxy_mod._call_upstream(make_ctx(), "nb-1", "x", {})
        assert result.is_error is True
        assert "get_jupyter_server(name='nb-1')" in tool_text(result)

    async def test_expired_token_asks_to_relink(
        self,
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await nb_proxy_mod._call_upstream(
            make_ctx(token="expired"), "nb-1", "x", {}
        )
        assert result.is_error is True
        assert "Re-link" in tool_text(result)

    async def test_hub_failure_is_formatted(
        self,
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = make_ctx()
        await nb_proxy_mod._call_upstream(ctx, "warm-the-auth-cache", "x", {})
        fake_hub.tokens.clear()  # the cached identity survives; get_user fails
        result = await nb_proxy_mod._call_upstream(ctx, "nb-1", "x", {})
        assert result.is_error is True
        assert "403" in tool_text(result)

    async def test_transport_error_gets_readiness_hint(
        self,
        make_ctx: Callable[..., MagicMock],
        ready_server: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        assert ready_server.servers["alice"]
        with patch(
            _PATCH, new=AsyncMock(side_effect=NotebookToolTransportError("unreachable"))
        ):
            result = await nb_proxy_mod._call_upstream(
                make_ctx(), "nb-1", "list_kernels", {}
            )
        output = tool_text(result)
        assert "unreachable" in output
        assert "get_jupyter_server" in output
        assert result.is_error is True

    async def test_upstream_error_is_formatted_without_extra_hints(
        self,
        make_ctx: Callable[..., MagicMock],
        ready_server: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        assert ready_server.servers["alice"]
        with patch(
            _PATCH,
            new=AsyncMock(side_effect=NotebookToolUpstreamError("kernel not found")),
        ):
            result = await nb_proxy_mod._call_upstream(
                make_ctx(), "nb-1", "list_kernels", {}
            )
        output = tool_text(result)
        assert "kernel not found" in output
        assert "**Try:**" not in output
        assert result.is_error is True


# ---------------------------------------------------------------------------
# Tests: jupyter-mcp-server 2.x argument forwarding
# ---------------------------------------------------------------------------


class TestNbProxyForwardsUpstreamArgs:
    """Each tool forwards exactly the jupyter-mcp-server 2.x arguments it was given.

    Optional upstream arguments (``cell_index``, ``cell_id``, ``notebook_name``,
    ...) are only forwarded when set, so upstream applies its own defaults and
    its own "given both, the id wins" resolution.
    """

    @pytest.mark.parametrize(
        ("tool", "call_args", "upstream", "expected_args"),
        [
            pytest.param(
                "nb_list_files",
                {},
                "list_files",
                {
                    "path": "",
                    "max_depth": 1,
                    "start_index": 0,
                    "limit": 25,
                    "pattern": "",
                },
                id="list_files-defaults",
            ),
            pytest.param("nb_list_kernels", {}, "list_kernels", {}, id="list_kernels"),
            pytest.param(
                "nb_list_notebooks", {}, "list_notebooks", {}, id="list_notebooks"
            ),
            pytest.param(
                "nb_use_notebook",
                {"notebook_name": "a", "notebook_path": "a.ipynb", "kernel_id": "k"},
                "use_notebook",
                {
                    "notebook_name": "a",
                    "notebook_path": "a.ipynb",
                    "mode": "connect",
                    "kernel_id": "k",
                },
                id="use_notebook",
            ),
            pytest.param(
                "nb_unuse_notebook",
                {"notebook_name": "a"},
                "unuse_notebook",
                {"notebook_name": "a"},
                id="unuse_notebook",
            ),
            pytest.param(
                "nb_restart_notebook",
                {"notebook_name": "a"},
                "restart_notebook",
                {"notebook_name": "a"},
                id="restart_notebook",
            ),
            pytest.param(
                "nb_read_notebook",
                {"notebook_name": "a"},
                "read_notebook",
                {
                    "notebook_name": "a",
                    "response_format": "brief",
                    "start_index": 0,
                    "limit": 20,
                },
                id="read_notebook",
            ),
            pytest.param(
                "nb_read_cell",
                {"cell_index": 3},
                "read_cell",
                {"cell_index": 3, "include_outputs": True},
                id="read_cell-by-index",
            ),
            pytest.param(
                "nb_read_cell",
                {"cell_id": "abc"},
                "read_cell",
                {"include_outputs": True, "cell_id": "abc"},
                id="read_cell-by-id",
            ),
            pytest.param(
                "nb_insert_cell",
                {"cell_index": 0, "cell_type": "code", "cell_source": "x"},
                "insert_cell",
                {"cell_index": 0, "cell_type": "code", "cell_source": "x"},
                id="insert_cell",
            ),
            pytest.param(
                "nb_overwrite_cell_source",
                {"cell_source": "x = 1", "cell_id": "abc"},
                "overwrite_cell_source",
                {"cell_source": "x = 1", "cell_id": "abc"},
                id="overwrite_cell_source-by-id",
            ),
            pytest.param(
                "nb_edit_cell_source",
                {"old_string": "a", "new_string": "b", "cell_id": "abc"},
                "edit_cell_source",
                {
                    "old_string": "a",
                    "new_string": "b",
                    "replace_all": False,
                    "cell_id": "abc",
                },
                id="edit_cell_source-by-id",
            ),
            pytest.param(
                "nb_delete_cell",
                {"cell_ids_to_delete": ["abc", "def"]},
                "delete_cell",
                {"include_source": True, "cell_ids_to_delete": ["abc", "def"]},
                id="delete_cell-by-ids",
            ),
            pytest.param(
                "nb_move_cell",
                {"source_cell_id": "abc", "target_cell_id": "def"},
                "move_cell",
                {"source_cell_id": "abc", "target_cell_id": "def"},
                id="move_cell-by-ids",
            ),
            pytest.param(
                "nb_execute_cell",
                {"cell_id": "abc"},
                "execute_cell",
                {
                    "timeout": 0,
                    "stream": True,
                    "progress_interval": 5,
                    "cell_id": "abc",
                },
                id="execute_cell-by-id",
            ),
            pytest.param(
                "nb_clear_cell_output",
                {"cell_index": 2, "notebook_name": "analysis"},
                "clear_cell_output",
                {"cell_index": 2, "notebook_name": "analysis"},
                id="clear_cell_output-by-index",
            ),
            pytest.param(
                "nb_insert_execute_code_cell",
                {"cell_index": 1, "cell_source": "x"},
                "insert_execute_code_cell",
                {
                    "cell_index": 1,
                    "cell_source": "x",
                    "timeout": 0,
                    "stream": True,
                    "progress_interval": 5,
                },
                id="insert_execute_code_cell",
            ),
            pytest.param(
                "nb_execute_code",
                {"code": "print(42)", "kernel_id": "k"},
                "execute_code",
                {
                    "code": "print(42)",
                    "timeout": 30,
                    "progress_interval": 5,
                    "kernel_id": "k",
                },
                id="execute_code",
            ),
        ],
    )
    async def test_forwards_args(
        self,
        registered_nb_tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        ready_server: FakeHub,
        tool: str,
        call_args: dict[str, object],
        upstream: str,
        expected_args: dict[str, object],
    ) -> None:
        assert ready_server.servers["alice"]
        with patch(_PATCH, new=AsyncMock(return_value="ok")) as mock_call:
            result = await registered_nb_tools[tool](
                server_name="nb-1", **call_args, ctx=make_ctx()
            )

        assert result.is_error is not True
        kwargs = mock_call.call_args.kwargs
        assert kwargs["tool_name"] == upstream
        assert kwargs["tool_args"] == expected_args
        assert kwargs["mcp_url"].endswith("/user/alice/nb-1/mcp")


# ---------------------------------------------------------------------------
# Tests: per-image tool overrides
# ---------------------------------------------------------------------------


class TestImageToolOverrides:
    """`_call_upstream` refuses tools a server's image is configured not to support.

    The image is the server's ``user_options.image``; images without an
    override (including BinderHub-built images) support every tool.
    """

    async def _call(
        self,
        make_ctx: Callable[..., MagicMock],
        settings: Settings,
        overrides: dict[str, frozenset[str]],
        tool: str = "nb_list_kernels",
    ) -> tuple[CallToolResult, AsyncMock]:
        ctx = make_ctx(
            settings_override=dataclasses.replace(
                settings, image_tool_overrides=overrides
            )
        )
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
        with patch(_PATCH, new=AsyncMock(return_value="ok")) as mock_call:
            result = await tools[tool](server_name="nb-1", ctx=ctx)
        return result, mock_call

    async def test_image_without_override_forwards(
        self,
        make_ctx: Callable[..., MagicMock],
        settings: Settings,
        ready_server: FakeHub,
    ) -> None:
        assert ready_server.servers["alice"]
        result, mock_call = await self._call(
            make_ctx, settings, {"some-other:image": frozenset()}
        )
        assert result.is_error is not True
        mock_call.assert_called_once()

    async def test_override_including_tool_forwards(
        self,
        make_ctx: Callable[..., MagicMock],
        settings: Settings,
        ready_server: FakeHub,
    ) -> None:
        assert ready_server.servers["alice"]
        result, mock_call = await self._call(
            make_ctx, settings, {CPU_IMAGE: frozenset({"list_kernels", "read_cell"})}
        )
        assert result.is_error is not True
        mock_call.assert_called_once()

    async def test_override_excluding_tool_is_a_clear_error(
        self,
        make_ctx: Callable[..., MagicMock],
        settings: Settings,
        ready_server: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        assert ready_server.servers["alice"]
        result, mock_call = await self._call(
            make_ctx, settings, {CPU_IMAGE: frozenset({"read_cell"})}
        )
        mock_call.assert_not_called()
        assert result.is_error is True
        output = tool_text(result)
        assert CPU_IMAGE in output
        assert "list_kernels" in output
        assert "list_supported_images" in output

    async def test_empty_override_blocks_every_tool(
        self,
        make_ctx: Callable[..., MagicMock],
        settings: Settings,
        ready_server: FakeHub,
    ) -> None:
        assert ready_server.servers["alice"]
        result, mock_call = await self._call(
            make_ctx, settings, {CPU_IMAGE: frozenset()}, tool="nb_list_notebooks"
        )
        mock_call.assert_not_called()
        assert result.is_error is True

    @pytest.mark.parametrize(
        "user_options",
        [
            pytest.param(
                {"image": "harbor.af.uchicago.edu/binderhub/ml-platform:abc"},
                id="binderhub-image",
            ),
            pytest.param({}, id="no-image"),
        ],
    )
    async def test_servers_without_a_matching_override_get_every_tool(
        self,
        make_ctx: Callable[..., MagicMock],
        settings: Settings,
        fake_hub: FakeHub,
        user_options: dict[str, Any],
    ) -> None:
        fake_hub.add_server("alice", "nb-1", user_options=user_options)
        result, mock_call = await self._call(
            make_ctx, settings, {CPU_IMAGE: frozenset()}
        )
        assert result.is_error is not True
        mock_call.assert_called_once()
