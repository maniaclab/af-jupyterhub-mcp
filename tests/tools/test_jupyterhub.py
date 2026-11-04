"""Tests for the seven server-lifecycle tools in tools/jupyterhub.py."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from mcp.server.mcpserver import MCPServer

from af_jupyterhub_mcp.tools import jupyterhub as jupyterhub_tools
from tests.conftest import CPU_IMAGE, GPU_IMAGE, PUBLIC_HUB_URL
from tests.fakes import BOB_TOKEN, FakeHub

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from unittest.mock import MagicMock

    from mcp.types import CallToolResult

    ToolFn = Callable[..., Awaitable[CallToolResult]]

_SECRET = "SECRET-binder-token"


@pytest.fixture
def tools() -> dict[str, ToolFn]:
    mcp = MCPServer("test")
    jupyterhub_tools.register(mcp)
    return {tool.name: tool.fn for tool in mcp._tool_manager.list_tools()}


def _posts(fake_hub: FakeHub) -> list[dict[str, Any]]:
    return [
        json.loads(r.content)
        for r in fake_hub.requests
        if r.method == "POST" and "/servers/" in r.url.path
    ]


class TestRegistration:
    _READ_ONLY: ClassVar[set[str]] = {
        "list_jupyter_servers",
        "get_jupyter_server",
        "list_supported_images",
        "list_sites",
    }
    _MUTATING: ClassVar[set[str]] = {"create_jupyter_server"}
    _DESTRUCTIVE: ClassVar[set[str]] = {"stop_jupyter_server", "delete_jupyter_server"}

    def test_registers_exactly_the_seven_tools(self) -> None:
        mcp = MCPServer("test")
        jupyterhub_tools.register(mcp)
        names = {t.name for t in mcp._tool_manager.list_tools()}
        assert names == self._READ_ONLY | self._MUTATING | self._DESTRUCTIVE

    def test_annotations_and_output_schemas(self) -> None:
        mcp = MCPServer("test")
        jupyterhub_tools.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            ann = tool.annotations
            assert ann is not None, tool.name
            assert tool.output_schema is not None, tool.name
            assert "ctx" not in tool.parameters.get("properties", {}), tool.name
            if tool.name in self._READ_ONLY:
                assert ann.read_only_hint is True, tool.name
                assert ann.open_world_hint is True, tool.name
            elif tool.name in self._MUTATING:
                assert ann.read_only_hint is False, tool.name
                assert ann.destructive_hint is None, tool.name
            else:
                assert ann.read_only_hint is False, tool.name
                assert ann.destructive_hint is True, tool.name

    def test_no_tool_takes_an_owner_argument(self) -> None:
        mcp = MCPServer("test")
        jupyterhub_tools.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            params = set(tool.parameters.get("properties", {}))
            assert not params & {"owner", "user", "username"}, tool.name

    def test_stop_description_explains_the_named_server_limit(self) -> None:
        mcp = MCPServer("test")
        jupyterhub_tools.register(mcp)
        by_name = {t.name: t for t in mcp._tool_manager.list_tools()}
        stop = by_name["stop_jupyter_server"].description
        assert "keep its named-server entry" in stop
        assert "pylint" not in stop
        assert "10" in stop
        assert "delete_jupyter_server" in stop
        assert "remove" in by_name["delete_jupyter_server"].description.lower()


class TestCreateJupyterServer:
    async def test_pending_spawn_is_followed_until_ready(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = make_ctx()
        result = await tools["create_jupyter_server"](image=CPU_IMAGE, ctx=ctx)

        assert result.is_error is not True, tool_text(result)
        server = result.structured_content["server"]
        assert server["name"] == "notebook-1"
        assert server["status"] == "ready"
        assert server["image"] == CPU_IMAGE
        assert server["url"] == f"{PUBLIC_HUB_URL}/user/alice/notebook-1/"
        assert "notebook-1" in tool_text(result)
        progress = [c.kwargs for c in ctx.report_progress.await_args_list]
        assert [p["progress"] for p in progress] == [10, 50, 100]
        assert all(p["total"] == 100 for p in progress)
        assert progress[1]["message"] == "Pulling image"
        assert fake_hub.servers["alice"]["notebook-1"]["ready"] is True

    async def test_sends_the_spawner_user_options(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
    ) -> None:
        await tools["create_jupyter_server"](
            image=GPU_IMAGE,
            name="gpu-work",
            site="ssl",
            cpu_cores=4,
            memory_gb=16,
            guaranteed_qos=True,
            gpus=1,
            gpu_product="NVIDIA-A100",
            ctx=make_ctx(),
        )
        assert _posts(fake_hub) == [
            {
                "image": GPU_IMAGE,
                "launched_by": "af-mcp-platform",
                "resource_requests": {
                    "site": "ssl",
                    "cpu": "4",
                    "memory": "16",
                    "qos": "Guaranteed",
                    "gpuCount": "1",
                    "gpuModel": "NVIDIA-A100",
                },
            }
        ]

    async def test_immediate_start_needs_no_progress_stream(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
    ) -> None:
        fake_hub.spawn_status = 201
        ctx = make_ctx()
        result = await tools["create_jupyter_server"](image=CPU_IMAGE, ctx=ctx)
        assert result.is_error is not True
        assert not any(r.url.path.endswith("/progress") for r in fake_hub.requests)
        ctx.report_progress.assert_not_awaited()

    async def test_generated_name_skips_existing_entries(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
    ) -> None:
        fake_hub.add_server("alice", "notebook-1", ready=False)
        result = await tools["create_jupyter_server"](image=CPU_IMAGE, ctx=make_ctx())
        assert result.structured_content["server"]["name"] == "notebook-2"

    async def test_existing_stopped_name_is_started_again(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
    ) -> None:
        fake_hub.add_server("alice", "mine", ready=False)
        result = await tools["create_jupyter_server"](
            image=CPU_IMAGE, name="mine", ctx=make_ctx()
        )
        assert result.is_error is not True
        assert fake_hub.servers["alice"]["mine"]["ready"] is True

    async def test_existing_active_name_is_a_conflict(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        fake_hub.add_server("alice", "mine")
        result = await tools["create_jupyter_server"](
            image=CPU_IMAGE, name="mine", ctx=make_ctx()
        )
        assert result.is_error is True
        assert "already" in tool_text(result)
        assert _posts(fake_hub) == []

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            pytest.param({"image": "docker.io/x:1"}, "allowlist", id="image"),
            pytest.param({"image": CPU_IMAGE, "gpus": 1}, "GPU image", id="gpu"),
            pytest.param({"image": CPU_IMAGE, "cpu_cores": 99}, "cpu_cores", id="cpu"),
            pytest.param({"image": CPU_IMAGE, "site": "mars"}, "mars", id="site"),
            pytest.param(
                {"image": CPU_IMAGE, "name": "Bad_Name"}, "Bad_Name", id="name"
            ),
        ],
    )
    async def test_guardrails_reject_before_contacting_the_hub(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
        kwargs: dict[str, Any],
        expected: str,
    ) -> None:
        result = await tools["create_jupyter_server"](**kwargs, ctx=make_ctx())
        assert result.is_error is True
        output = tool_text(result)
        assert expected in output
        assert "list_supported_images" in output
        assert _posts(fake_hub) == []

    async def test_named_server_limit_is_a_clear_error(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        fake_hub.named_server_limit = 1
        fake_hub.add_server("alice", "old", ready=False)
        result = await tools["create_jupyter_server"](image=CPU_IMAGE, ctx=make_ctx())
        assert result.is_error is True
        output = tool_text(result)
        assert "maximum of 1 named servers" in output
        assert "delete_jupyter_server" in output

    async def test_failed_spawn_is_a_clear_error(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        fake_hub.progress_events = [
            {"progress": 100, "failed": True, "message": "Spawn failed: no nodes"}
        ]
        result = await tools["create_jupyter_server"](image=CPU_IMAGE, ctx=make_ctx())
        assert result.is_error is True
        output = tool_text(result)
        assert "no nodes" in output
        assert "delete_jupyter_server" in output

    async def test_timeout_leaves_the_server_pending(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        settings: Any,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        fake_hub.progress_hangs = True
        quick = type(settings)(**{**settings.__dict__, "launch_timeout_seconds": 1})
        result = await tools["create_jupyter_server"](
            image=CPU_IMAGE, ctx=make_ctx(settings_override=quick)
        )
        assert result.is_error is True
        output = tool_text(result)
        assert "not ready after 1 seconds" in output
        assert "get_jupyter_server" in output
        assert fake_hub.servers["alice"]["notebook-1"]["pending"] == "spawn"

    async def test_invalid_token_asks_to_relink(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await tools["create_jupyter_server"](
            image=CPU_IMAGE, ctx=make_ctx(token="expired")
        )
        assert result.is_error is True
        assert "re-link" in tool_text(result)


class TestListAndGet:
    @pytest.fixture
    def populated(self, fake_hub: FakeHub) -> FakeHub:
        fake_hub.add_server(
            "alice",
            "notebook-1",
            user_options={
                "image": CPU_IMAGE,
                "resource_requests": {"site": "local", "cpu": "1", "memory": "1"},
            },
        )
        fake_hub.add_server(
            "alice",
            "maniaclab-ml_platform-ab12cd34",
            user_options={
                "token": _SECRET,
                "image": "harbor.af.uchicago.edu/binderhub/ml-platform:abc",
                "binder_request": "v2/gh/maniaclab/ml_platform/main",
            },
        )
        fake_hub.add_server("alice", "parked", ready=False)
        fake_hub.add_server("bob", "bobs")
        return fake_hub

    async def test_list_shows_every_own_server_without_tokens(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        populated: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await tools["list_jupyter_servers"](ctx=make_ctx())
        names = [s["name"] for s in result.structured_content["servers"]]
        assert sorted(names) == [
            "maniaclab-ml_platform-ab12cd34",
            "notebook-1",
            "parked",
        ]
        assert _SECRET not in tool_text(result)
        assert _SECRET not in json.dumps(result.structured_content)
        assert "bobs" not in tool_text(result)
        assert populated.requests[-1].url.path == "/hub/api/users/alice"

    async def test_list_with_no_servers(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await tools["list_jupyter_servers"](ctx=make_ctx())
        assert "No servers found." in tool_text(result)
        assert result.structured_content == {"servers": []}

    async def test_get_binderhub_ui_server(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        populated: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        assert populated.servers["alice"]
        result = await tools["get_jupyter_server"](
            name="maniaclab-ml_platform-ab12cd34", ctx=make_ctx()
        )
        server = result.structured_content["server"]
        assert server["binder_request"] == "v2/gh/maniaclab/ml_platform/main"
        assert server["status"] == "ready"
        assert _SECRET not in tool_text(result)
        assert _SECRET not in json.dumps(result.structured_content)

    async def test_get_another_users_server_is_not_found(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        populated: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        assert "bobs" in populated.servers["bob"]
        result = await tools["get_jupyter_server"](name="bobs", ctx=make_ctx())
        assert result.is_error is True
        assert "list_jupyter_servers" in tool_text(result)

    async def test_get_rejects_unaddressable_names(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await tools["get_jupyter_server"](name="a/b", ctx=make_ctx())
        assert result.is_error is True
        assert "server name" in tool_text(result)


class TestStopAndDelete:
    async def test_stop_keeps_the_entry(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        fake_hub.add_server("alice", "nb")
        result = await tools["stop_jupyter_server"](name="nb", ctx=make_ctx())
        assert result.is_error is not True
        assert result.structured_content == {"name": "nb", "stopped": True}
        assert fake_hub.servers["alice"]["nb"]["stopped"] is True
        assert "delete_jupyter_server" in tool_text(result)

    async def test_delete_removes_the_entry(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
    ) -> None:
        fake_hub.add_server("alice", "nb")
        result = await tools["delete_jupyter_server"](name="nb", ctx=make_ctx())
        assert result.is_error is not True
        assert result.structured_content == {"name": "nb", "deleted": True}
        assert "nb" not in fake_hub.servers["alice"]

    @pytest.mark.parametrize("tool", ["stop_jupyter_server", "delete_jupyter_server"])
    async def test_unknown_server_is_not_found(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
        tool: str,
    ) -> None:
        fake_hub.add_server("bob", "nb")
        result = await tools[tool](name="nb", ctx=make_ctx())
        assert result.is_error is True
        assert "list_jupyter_servers" in tool_text(result)
        assert "nb" in fake_hub.servers["bob"]

    async def test_other_users_token_cannot_delete(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
    ) -> None:
        fake_hub.add_server("alice", "nb")
        result = await tools["delete_jupyter_server"](
            name="nb", ctx=make_ctx(token=BOB_TOKEN)
        )
        assert result.is_error is True
        assert "nb" in fake_hub.servers["alice"]


class TestStaticListings:
    async def test_list_supported_images(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await tools["list_supported_images"](ctx=make_ctx())
        assert result.structured_content == {
            "cpu_images": [CPU_IMAGE],
            "gpu_images": [GPU_IMAGE],
        }
        assert "CPU images" in tool_text(result)

    async def test_list_sites(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        result = await tools["list_sites"](ctx=make_ctx())
        assert result.structured_content == {"sites": ["local", "ssl"]}
        assert "- ssl" in tool_text(result)


_HUB_CALLING: list[tuple[str, dict[str, Any], str]] = [
    ("create_jupyter_server", {"image": CPU_IMAGE}, "/hub/api/users/alice"),
    ("list_jupyter_servers", {}, "/hub/api/users/alice"),
    ("get_jupyter_server", {"name": "nb"}, "/hub/api/users/alice"),
    ("stop_jupyter_server", {"name": "nb"}, "/hub/api/users/alice/servers"),
    ("delete_jupyter_server", {"name": "nb"}, "/hub/api/users/alice/servers"),
]


class TestHubFailuresAreFormatted:
    @pytest.mark.parametrize(("tool", "args", "path"), _HUB_CALLING)
    async def test_expired_link_asks_to_relink(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        tool_text: Callable[[CallToolResult], str],
        tool: str,
        args: dict[str, Any],
        path: str,
    ) -> None:
        assert path
        result = await tools[tool](**args, ctx=make_ctx(token="expired"))
        assert result.is_error is True
        assert "re-link" in tool_text(result).lower()

    @pytest.mark.parametrize(("tool", "args", "path"), _HUB_CALLING)
    async def test_hub_server_error_is_surfaced(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
        tool: str,
        args: dict[str, Any],
        path: str,
    ) -> None:
        fake_hub.fail_paths[path] = 500
        result = await tools[tool](**args, ctx=make_ctx())
        assert result.is_error is True
        assert "JupyterHub returned 500: hub is unhappy" in tool_text(result)

    @pytest.mark.parametrize(("tool", "args", "path"), _HUB_CALLING)
    async def test_hub_forbidden_asks_to_relink(
        self,
        tools: dict[str, ToolFn],
        make_ctx: Callable[..., MagicMock],
        fake_hub: FakeHub,
        tool_text: Callable[[CallToolResult], str],
        tool: str,
        args: dict[str, Any],
        path: str,
    ) -> None:
        fake_hub.fail_paths[path] = 403
        result = await tools[tool](**args, ctx=make_ctx())
        assert result.is_error is True
        assert "Re-link" in tool_text(result)
