"""Proxy tools that forward jupyter-mcp-server tool calls through jupyterhub-mcp.

Each tool here mirrors one tool from jupyter-mcp-server
(https://github.com/datalayer/jupyter-mcp-server). The tool:

1. Takes ``server_name: str`` (one of the caller's named servers) as its
   first required argument.
2. Looks the server up in the caller's own hub user model, so a server that
   is not the caller's is indistinguishable from one that does not exist.
3. Checks the server is ready (not pending, not stopped); returns an error
   with a hint if not.
4. Calls the upstream jupyter-mcp-server tool via ``call_notebook_tool``
   at ``{hub_url}/user/<name>/<server>/mcp``, authenticated by the caller's
   own hub token in a header -- never returned to the caller.

Tool signatures are copied from jupyter-mcp-server v2.x source; see
https://github.com/datalayer/jupyter-mcp-server/tree/main/jupyter_mcp_server/tools

The JupyterLab frontend commands served by the ``jupyter-mcp-tools``
extension (``notebook_get-selected-cell``, ``notebook_run-all-cells``, ...)
are proxied separately as ``nb_ui_*`` tools in ``nb_ui.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from af_jupyterhub_mcp.auth.hub import HubIdentity, get_hub_identity
from af_jupyterhub_mcp.hub.errors import (
    GuardrailError,
    HubError,
    NotebookNotReadyError,
    NotFoundOrNotYoursError,
    ToolNotSupportedByImageError,
)
from af_jupyterhub_mcp.hub.proxy import (
    NotebookToolTransportError,
    NotebookToolUpstreamError,
    call_notebook_tool,
)
from af_jupyterhub_mcp.hub.servers import check_server_name, find_server, server_status
from af_jupyterhub_mcp.tools._helpers import format_error

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from af_jupyterhub_mcp.config import Settings
    from af_jupyterhub_mcp.hub.client import HubClient


class NbProxyResult(BaseModel):
    """Structured result of a proxied nb_* tool call.

    jupyter-mcp-server (the upstream every nb_* tool proxies to) returns
    markdown/plain text for every tool, not structured data -- this is a
    minimal wrapper around that opaque passthrough rather than a per-tool
    schema, since there is no further structure to extract without coupling
    to upstream's undocumented text format.
    """

    result: str


def _lifespan(ctx: Any) -> tuple[HubClient, Settings]:
    lc = ctx.request_context.lifespan_context
    return lc["hub"], lc["settings"]


async def _get_ready_server(
    ctx: Any,
    server_name: str,
) -> tuple[HubIdentity, dict[str, Any]]:
    """Resolve the caller's *server_name* and check that it is ready.

    Returns ``(identity, server model)`` on success. Raises
    ``GuardrailError`` (unaddressable name), ``NotFoundOrNotYoursError``
    (no such server of the caller's), ``NotebookNotReadyError`` (pending or
    stopped), or the auth/hub errors from ``get_hub_identity``/``get_user``
    -- callers are expected to catch these and format them via
    ``format_error`` (never a bare string sniffed by ``isinstance``).
    """
    hub, _ = _lifespan(ctx)
    check_server_name(server_name)
    identity = await get_hub_identity(ctx)
    user = await hub.get_user(identity.token, identity.name)
    model = find_server(user, server_name)
    if model is None:
        msg = f"no server named {server_name!r} (or it is not yours)"
        raise NotFoundOrNotYoursError(msg)
    if model.get("pending") or not model.get("ready"):
        msg = f"server {server_name!r} is not ready ({server_status(model)})"
        raise NotebookNotReadyError(msg)
    return identity, model


async def _call_upstream(
    ctx: Any,
    server_name: str,
    tool_name: str,
    tool_args: dict[str, object],
) -> CallToolResult:
    """Resolve the caller's server, forward *tool_name* upstream, and wrap its output.

    Shared by all ``nb_*`` and ``nb_ui_*`` tools: each just builds its own
    ``tool_args`` dict and delegates here. Every failure mode (not found/not
    yours, not ready, auth, hub, transport error, upstream tool error) is
    caught here and turned into a single ``is_error`` result via
    ``format_error`` -- this is the one place that knows the recovery hint
    for each failure. On success, the upstream's text output is wrapped in
    ``NbProxyResult`` for ``structured_content``.
    """
    try:
        identity, model = await _get_ready_server(ctx, server_name)
    except (NotFoundOrNotYoursError, GuardrailError) as exc:
        return format_error(
            exc, hints=["Use `list_jupyter_servers` to see your own servers."]
        )
    except NotebookNotReadyError as exc:
        return format_error(
            exc,
            hints=[
                f"Use `get_jupyter_server(name={server_name!r})` to check readiness.",
                (
                    "Wait for the server to become ready (or start it again with"
                    " `create_jupyter_server`) before calling notebook tools."
                ),
            ],
        )
    except PermissionError as exc:
        return format_error(
            exc,
            hints=["Re-link your JupyterHub account in af-mcp-platform, then retry."],
        )
    except HubError as exc:
        return format_error(exc)

    hub, settings = _lifespan(ctx)
    image = (model.get("user_options") or {}).get("image")
    supported = (
        settings.image_tool_overrides.get(image) if isinstance(image, str) else None
    )
    if supported is not None and tool_name not in supported:
        msg = (
            f"Server {server_name!r} runs image {image!r}, which does not "
            f"offer {tool_name!r} (supported: {', '.join(sorted(supported)) or 'none'})"
        )
        return format_error(
            ToolNotSupportedByImageError(msg),
            hints=[
                (
                    "Use `list_supported_images` and start a new server on an "
                    "image that supports this tool."
                ),
            ],
        )
    try:
        async with hub.notebook_http_client(identity.token) as http_client:
            text = await call_notebook_tool(
                http_client=http_client,
                mcp_url=hub.notebook_mcp_url(identity.name, server_name),
                tool_name=tool_name,
                tool_args=tool_args,
            )
    except NotebookToolTransportError as exc:
        return format_error(
            exc,
            hints=[
                "Check that the server is ready with `get_jupyter_server`.",
                "Check that the image runs jupyter-mcp-server (see `get_jupyter_server`).",
            ],
        )
    except NotebookToolUpstreamError as exc:
        return format_error(exc)

    payload = NbProxyResult(result=text)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload.model_dump(mode="json"),
    )


def register(mcp: MCPServer) -> None:
    """Register the 17 nb_* proxy tools on *mcp*."""
    # ------------------------------------------------------------------
    # Filesystem
    # ------------------------------------------------------------------

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List files",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def nb_list_files(
        server_name: str,
        path: str = "",
        max_depth: int = 1,
        start_index: int = 0,
        limit: int = 25,
        pattern: str = "",
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """List files on the notebook server's filesystem."""
        return await _call_upstream(
            ctx,
            server_name,
            "list_files",
            {
                "path": path,
                "max_depth": max_depth,
                "start_index": start_index,
                "limit": limit,
                "pattern": pattern,
            },
        )

    # ------------------------------------------------------------------
    # Kernel management
    # ------------------------------------------------------------------

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List kernels",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def nb_list_kernels(
        server_name: str,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """List all running kernels on the notebook server."""
        return await _call_upstream(ctx, server_name, "list_kernels", {})

    # ------------------------------------------------------------------
    # Notebook management
    # ------------------------------------------------------------------

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List notebooks",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def nb_list_notebooks(
        server_name: str,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """List notebooks open on the notebook server."""
        return await _call_upstream(ctx, server_name, "list_notebooks", {})

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Use notebook",
            read_only_hint=False,
        )
    )
    async def nb_use_notebook(
        server_name: str,
        notebook_name: str,
        notebook_path: str,
        mode: Literal["connect", "create"] = "connect",
        kernel_id: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Connect to or create a notebook on the notebook server."""
        args: dict[str, Any] = {
            "notebook_name": notebook_name,
            "notebook_path": notebook_path,
            "mode": mode,
        }
        if kernel_id is not None:
            args["kernel_id"] = kernel_id
        return await _call_upstream(ctx, server_name, "use_notebook", args)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Unuse notebook",
            read_only_hint=False,
        )
    )
    async def nb_unuse_notebook(
        server_name: str,
        notebook_name: str,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Disconnect from a notebook on the notebook server."""
        return await _call_upstream(
            ctx, server_name, "unuse_notebook", {"notebook_name": notebook_name}
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Restart notebook",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def nb_restart_notebook(
        server_name: str,
        notebook_name: str,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Restart a notebook's kernel on the notebook server."""
        return await _call_upstream(
            ctx,
            server_name,
            "restart_notebook",
            {"notebook_name": notebook_name},
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Read notebook",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def nb_read_notebook(
        server_name: str,
        notebook_name: str,
        response_format: Literal["brief", "detailed"] = "brief",
        start_index: int = 0,
        limit: int = 20,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Read a notebook's cells from the notebook server."""
        return await _call_upstream(
            ctx,
            server_name,
            "read_notebook",
            {
                "notebook_name": notebook_name,
                "response_format": response_format,
                "start_index": start_index,
                "limit": limit,
            },
        )

    # ------------------------------------------------------------------
    # Cell reading
    # ------------------------------------------------------------------

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Read cell",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def nb_read_cell(
        server_name: str,
        cell_index: int | None = None,
        include_outputs: bool = True,
        notebook_name: str | None = None,
        cell_id: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Read a cell from the active notebook on the notebook server.

        Address the cell by cell_index or cell_id (given both, the id wins).
        """
        args: dict[str, Any] = {"include_outputs": include_outputs}
        if cell_index is not None:
            args["cell_index"] = cell_index
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        if cell_id is not None:
            args["cell_id"] = cell_id
        return await _call_upstream(ctx, server_name, "read_cell", args)

    # ------------------------------------------------------------------
    # Cell writing
    # ------------------------------------------------------------------

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Insert cell",
            read_only_hint=False,
        )
    )
    async def nb_insert_cell(
        server_name: str,
        cell_index: int,
        cell_type: Literal["code", "markdown"],
        cell_source: str,
        notebook_name: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Insert a cell at a given index in the active notebook."""
        args: dict[str, Any] = {
            "cell_index": cell_index,
            "cell_type": cell_type,
            "cell_source": cell_source,
        }
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        return await _call_upstream(ctx, server_name, "insert_cell", args)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Overwrite cell source",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def nb_overwrite_cell_source(
        server_name: str,
        cell_source: str,
        cell_index: int | None = None,
        notebook_name: str | None = None,
        cell_id: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Overwrite the source of a cell in the active notebook.

        Address the cell by cell_index or cell_id (given both, the id wins).
        """
        args: dict[str, Any] = {"cell_source": cell_source}
        if cell_index is not None:
            args["cell_index"] = cell_index
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        if cell_id is not None:
            args["cell_id"] = cell_id
        return await _call_upstream(ctx, server_name, "overwrite_cell_source", args)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Edit cell source",
            read_only_hint=False,
        )
    )
    async def nb_edit_cell_source(
        server_name: str,
        old_string: str,
        new_string: str,
        cell_index: int | None = None,
        replace_all: bool = False,
        notebook_name: str | None = None,
        cell_id: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Edit part of a cell's source in the active notebook.

        Address the cell by cell_index or cell_id (given both, the id wins).
        """
        args: dict[str, Any] = {
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
        }
        if cell_index is not None:
            args["cell_index"] = cell_index
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        if cell_id is not None:
            args["cell_id"] = cell_id
        return await _call_upstream(ctx, server_name, "edit_cell_source", args)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Delete cell",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def nb_delete_cell(
        server_name: str,
        cell_indices: list[int] | None = None,
        include_source: bool = True,
        notebook_name: str | None = None,
        cell_ids_to_delete: list[str] | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Delete one or more cells from the active notebook.

        Address the cells by cell_indices or cell_ids_to_delete (given both,
        the ids win; ids are safer since indices shift as earlier cells go).
        """
        args: dict[str, Any] = {"include_source": include_source}
        if cell_indices is not None:
            args["cell_indices"] = cell_indices
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        if cell_ids_to_delete is not None:
            args["cell_ids_to_delete"] = cell_ids_to_delete
        return await _call_upstream(ctx, server_name, "delete_cell", args)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Move cell",
            read_only_hint=False,
        )
    )
    async def nb_move_cell(
        server_name: str,
        source_index: int | None = None,
        target_index: int | None = None,
        notebook_name: str | None = None,
        source_cell_id: str | None = None,
        target_cell_id: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Move a cell to a different position in the active notebook.

        Address the source by source_index or source_cell_id, and the
        destination by target_index or target_cell_id (given both, the id wins).
        """
        args: dict[str, Any] = {}
        if source_index is not None:
            args["source_index"] = source_index
        if target_index is not None:
            args["target_index"] = target_index
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        if source_cell_id is not None:
            args["source_cell_id"] = source_cell_id
        if target_cell_id is not None:
            args["target_cell_id"] = target_cell_id
        return await _call_upstream(ctx, server_name, "move_cell", args)

    # ------------------------------------------------------------------
    # Cell execution
    # ------------------------------------------------------------------

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Execute cell",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def nb_execute_cell(
        server_name: str,
        cell_index: int | None = None,
        timeout: int = 0,
        stream: bool = True,
        progress_interval: int = 5,
        cell_id: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Execute a specific cell in the active notebook.

        Address the cell by cell_index or cell_id (given both, the id wins).
        """
        args: dict[str, Any] = {
            "timeout": timeout,
            "stream": stream,
            "progress_interval": progress_interval,
        }
        if cell_index is not None:
            args["cell_index"] = cell_index
        if cell_id is not None:
            args["cell_id"] = cell_id
        return await _call_upstream(ctx, server_name, "execute_cell", args)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Clear cell output",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def nb_clear_cell_output(
        server_name: str,
        cell_index: int | None = None,
        notebook_name: str | None = None,
        cell_id: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Clear a code cell's outputs and execution count, keeping the cell.

        Address the cell by cell_index or cell_id (given both, the id wins).
        """
        args: dict[str, Any] = {}
        if cell_index is not None:
            args["cell_index"] = cell_index
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        if cell_id is not None:
            args["cell_id"] = cell_id
        return await _call_upstream(ctx, server_name, "clear_cell_output", args)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Insert and execute code cell",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def nb_insert_execute_code_cell(
        server_name: str,
        cell_index: int,
        cell_source: str,
        timeout: int = 0,
        stream: bool = True,
        progress_interval: int = 5,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Insert a code cell and immediately execute it in the active notebook."""
        return await _call_upstream(
            ctx,
            server_name,
            "insert_execute_code_cell",
            {
                "cell_index": cell_index,
                "cell_source": cell_source,
                "timeout": timeout,
                "stream": stream,
                "progress_interval": progress_interval,
            },
        )

    # ------------------------------------------------------------------
    # Other tools
    # ------------------------------------------------------------------

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Execute code",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def nb_execute_code(
        server_name: str,
        code: str,
        timeout: int = 30,
        kernel_id: str | None = None,
        progress_interval: int = 5,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        """Execute arbitrary code in the notebook server's kernel.

        kernel_id is optional -- omitted, jupyter-mcp-server reuses the
        current notebook's kernel or starts a new one on demand, so no prior
        step is required. Use nb_list_kernels to see what kernels currently
        exist on the server.
        """
        args: dict[str, Any] = {
            "code": code,
            "timeout": timeout,
            "progress_interval": progress_interval,
        }
        if kernel_id is not None:
            args["kernel_id"] = kernel_id
        return await _call_upstream(ctx, server_name, "execute_code", args)
