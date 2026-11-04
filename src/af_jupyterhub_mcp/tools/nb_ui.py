"""Proxy tools for JupyterLab frontend (UI) commands served by jupyter-mcp-tools.

The notebook images ship the ``jupyter-mcp-tools`` JupyterLab extension
(https://github.com/datalayer/jupyter-mcp-tools) and allowlist these commands
via ``allowed_jupyter_mcp_tools`` (maniaclab/ml_platform#14). Each upstream
tool id is a JupyterLab command id with ``:`` replaced by ``_`` (e.g.
``notebook:run-all-cells`` -> ``notebook_run-all-cells``), and is exposed here
as ``nb_ui_`` + the id with ``-`` replaced by ``_``. The ``nb_ui_`` prefix
keeps these apart from the server-side ``nb_*`` tools in ``nb_proxy.py``
(``notebook_delete-cell`` would otherwise collide with ``nb_delete_cell``).

Unlike the ``nb_*`` tools, these execute inside the user's open JupyterLab
browser tab -- jupyter-mcp-server relays the call over a websocket to the
frontend, which runs the command against the active widget/selection. With no
tab open, the call fails.

Almost every command takes no arguments, so those are one row each in
``_UI_TOOLS`` registered by a small factory; the few that take arguments get
hand-written typed wrappers. All go through ``nb_proxy._call_upstream`` for
ownership, readiness, per-image tool support, the hub token header, and
error formatting.

Deliberately not proxied (and not allowlisted in the image): commands that act
on the human's browser rather than returning anything to the caller
(``filebrowser_upload``/``filebrowser_download``), and commands that block on
a modal dialog needing a human click (``docmanager_delete``/``_rename``/
``_save-as``, ``kernelmenu_change``/``_restart``, ``console_restart-kernel``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002
from mcp.types import CallToolResult, ToolAnnotations

from af_jupyterhub_mcp.tools.nb_proxy import NbProxyResult, _call_upstream

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

Kind = Literal["read_only", "mutating", "destructive"]

OPEN_TAB_NOTE = (
    "Acts on the user's open JupyterLab browser tab; fails if no tab is open."
)


@dataclass(frozen=True)
class UiToolSpec:
    """One argument-free JupyterLab command proxied as an ``nb_ui_*`` tool."""

    upstream: str
    title: str
    description: str
    kind: Kind

    @property
    def name(self) -> str:
        """Proxy tool name, derived from the upstream id so the two cannot drift."""
        return _ui_name(self.upstream)


def _ui_name(upstream: str) -> str:
    return "nb_ui_" + upstream.replace("-", "_")


def _annotations(title: str, kind: Kind) -> ToolAnnotations:
    """Map a read-only/mutating/destructive kind to ToolAnnotations.

    "Read-only" here includes commands that only change view state (cursor,
    selection, layout, search, theme) -- they leave notebook content, kernels,
    and files untouched. Commands that execute code are destructive, matching
    the server-side ``nb_execute_*`` tools.
    """
    if kind == "read_only":
        return ToolAnnotations(title=title, read_only_hint=True, open_world_hint=True)
    if kind == "mutating":
        return ToolAnnotations(title=title, read_only_hint=False)
    return ToolAnnotations(title=title, read_only_hint=False, destructive_hint=True)


def _description(text: str) -> str:
    return f"{OPEN_TAB_NOTE} {text}"


_UI_TOOLS: tuple[UiToolSpec, ...] = (
    # notebook: selection and cursor
    UiToolSpec(
        "notebook_get-selected-cell",
        "Get selected cell",
        "Return the currently selected cell of the active notebook.",
        "read_only",
    ),
    UiToolSpec(
        "notebook_move-cursor-down",
        "Select next cell",
        "Move the cell selection down by one in the active notebook.",
        "read_only",
    ),
    UiToolSpec(
        "notebook_move-cursor-up",
        "Select previous cell",
        "Move the cell selection up by one in the active notebook.",
        "read_only",
    ),
    UiToolSpec(
        "notebook_extend-marked-cells-below",
        "Extend selection below",
        "Extend the cell selection one cell down in the active notebook.",
        "read_only",
    ),
    UiToolSpec(
        "notebook_extend-marked-cells-above",
        "Extend selection above",
        "Extend the cell selection one cell up in the active notebook.",
        "read_only",
    ),
    UiToolSpec(
        "notebook_copy-cell",
        "Copy cells",
        "Copy the selected cells of the active notebook to the clipboard.",
        "read_only",
    ),
    # notebook: editing
    UiToolSpec(
        "notebook_insert-cell-below",
        "Insert cell below",
        "Insert an empty cell below the selected cell of the active notebook.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_insert-cell-above",
        "Insert cell above",
        "Insert an empty cell above the selected cell of the active notebook.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_paste-cell-below",
        "Paste cells below",
        "Paste clipboard cells below the selected cell of the active notebook.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_paste-cell-above",
        "Paste cells above",
        "Paste clipboard cells above the selected cell of the active notebook.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_move-cell-up",
        "Move cells up",
        "Move the selected cells of the active notebook up by one.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_move-cell-down",
        "Move cells down",
        "Move the selected cells of the active notebook down by one.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_split-cell-at-cursor",
        "Split cell",
        "Split the selected cell of the active notebook at the editor cursor.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_change-cell-to-code",
        "Change to code cell",
        "Change the selected cells of the active notebook to code cells.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_change-cell-to-markdown",
        "Change to markdown cell",
        "Change the selected cells of the active notebook to markdown cells.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_change-cell-to-raw",
        "Change to raw cell",
        "Change the selected cells of the active notebook to raw cells.",
        "mutating",
    ),
    UiToolSpec(
        "notebook_delete-cell",
        "Delete cells",
        "Delete the selected cells of the active notebook.",
        "destructive",
    ),
    UiToolSpec(
        "notebook_cut-cell",
        "Cut cells",
        "Cut the selected cells of the active notebook to the clipboard.",
        "destructive",
    ),
    UiToolSpec(
        "notebook_merge-cell-above",
        "Merge with cell above",
        "Merge the selected cell of the active notebook into the cell above.",
        "destructive",
    ),
    UiToolSpec(
        "notebook_merge-cell-below",
        "Merge with cell below",
        "Merge the selected cell of the active notebook with the cell below.",
        "destructive",
    ),
    # notebook: execution
    UiToolSpec(
        "notebook_run-all-cells",
        "Run all cells",
        "Run every cell of the active notebook in order.",
        "destructive",
    ),
    UiToolSpec(
        "notebook_run-cell",
        "Run selected cells",
        "Run the selected cells of the active notebook.",
        "destructive",
    ),
    UiToolSpec(
        "notebook_run-cell-and-select-next",
        "Run cell and select next",
        "Run the selected cells of the active notebook and select the next cell.",
        "destructive",
    ),
    UiToolSpec(
        "notebook_run-cell-and-insert-below",
        "Run cell and insert below",
        "Run the selected cells of the active notebook and insert a cell below.",
        "destructive",
    ),
    # console
    UiToolSpec(
        "console_clear",
        "Clear console",
        "Clear the cells of the active console.",
        "destructive",
    ),
    UiToolSpec(
        "console_interrupt-kernel",
        "Interrupt console kernel",
        "Interrupt the kernel of the active console.",
        "destructive",
    ),
    # document management
    UiToolSpec(
        "docmanager_save",
        "Save document",
        "Save the active document. Saving an untitled document may prompt the"
        " user to rename it in the tab.",
        "mutating",
    ),
    UiToolSpec(
        "docmanager_duplicate",
        "Duplicate document",
        "Duplicate the active document's file next to it.",
        "mutating",
    ),
    # file browser
    UiToolSpec(
        "filebrowser_refresh",
        "Refresh file browser",
        "Refresh the file browser listing.",
        "read_only",
    ),
    UiToolSpec(
        "filebrowser_toggle-hidden-files",
        "Toggle hidden files",
        "Show or hide hidden files in the file browser.",
        "read_only",
    ),
    UiToolSpec(
        "filebrowser_create-new-directory",
        "New folder",
        "Create a new untitled folder in the file browser's current directory.",
        "mutating",
    ),
    # kernel (acts on the active notebook or console)
    UiToolSpec(
        "kernelmenu_interrupt",
        "Interrupt kernel",
        "Interrupt the kernel of the active notebook or console.",
        "destructive",
    ),
    UiToolSpec(
        "kernelmenu_shutdown",
        "Shut down kernel",
        "Shut down the kernel of the active notebook or console.",
        "destructive",
    ),
    UiToolSpec(
        "kernelmenu_reconnect-to-kernel",
        "Reconnect to kernel",
        "Reconnect the active notebook or console to its kernel.",
        "mutating",
    ),
    # UI / layout
    UiToolSpec(
        "application_toggle-left-area",
        "Toggle left sidebar",
        "Show or hide the left sidebar.",
        "read_only",
    ),
    UiToolSpec(
        "application_toggle-right-area",
        "Toggle right sidebar",
        "Show or hide the right sidebar.",
        "read_only",
    ),
    UiToolSpec(
        "application_toggle-presentation-mode",
        "Toggle presentation mode",
        "Turn presentation mode on or off.",
        "read_only",
    ),
    UiToolSpec(
        "editmenu_open",
        "Open Edit menu",
        "Open the Edit menu.",
        "read_only",
    ),
    UiToolSpec(
        "filemenu_open",
        "Open File menu",
        "Open the File menu.",
        "read_only",
    ),
    UiToolSpec(
        "helpmenu_open",
        "Open Help menu",
        "Open the Help menu.",
        "read_only",
    ),
    # search
    UiToolSpec(
        "documentsearch_highlightNext",
        "Find next",
        "Highlight the next search match in the active document.",
        "read_only",
    ),
    UiToolSpec(
        "documentsearch_highlightPrevious",
        "Find previous",
        "Highlight the previous search match in the active document.",
        "read_only",
    ),
    # terminal
    UiToolSpec(
        "terminal_create-new",
        "New terminal",
        "Open a new terminal tab.",
        "mutating",
    ),
    UiToolSpec(
        "terminal_refresh",
        "Refresh terminal",
        "Refresh the active terminal.",
        "mutating",
    ),
)


def _register_arg_free(mcp: MCPServer, spec: UiToolSpec) -> None:
    """Register one argument-free ``nb_ui_*`` tool built from *spec*.

    A separate function (not a loop body) so each closure binds its own *spec*.
    """

    async def tool(
        server_name: str,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        return await _call_upstream(ctx, server_name, spec.upstream, {})

    mcp.tool(
        name=spec.name,
        description=_description(spec.description),
        annotations=_annotations(spec.title, spec.kind),
    )(tool)


def register(mcp: MCPServer) -> None:
    """Register the ``nb_ui_*`` frontend-command proxy tools on *mcp*."""
    for spec in _UI_TOOLS:
        _register_arg_free(mcp, spec)

    @mcp.tool(
        name=_ui_name("notebook_append-execute"),
        description=_description(
            "Append a cell at the end of the active notebook and execute it."
        ),
        annotations=_annotations("Append and execute cell", "destructive"),
    )
    async def nb_ui_notebook_append_execute(
        server_name: str,
        source: str,
        cell_type: Literal["code", "markdown", "raw"] = "code",
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        return await _call_upstream(
            ctx,
            server_name,
            "notebook_append-execute",
            {"source": source, "type": cell_type},
        )

    @mcp.tool(
        name=_ui_name("console_create"),
        description=_description("Open a new code console."),
        annotations=_annotations("New console", "mutating"),
    )
    async def nb_ui_console_create(
        server_name: str,
        path: str | None = None,
        insert_mode: Literal[
            "split-right", "split-left", "split-top", "split-bottom"
        ] = "split-right",
        activate: bool = True,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        args: dict[str, Any] = {"activate": activate, "insertMode": insert_mode}
        if path is not None:
            args["path"] = path
        return await _call_upstream(ctx, server_name, "console_create", args)

    @mcp.tool(
        name=_ui_name("console_inject"),
        description=_description(
            "Execute code in a console (the active one, or the console at path)."
        ),
        annotations=_annotations("Run code in console", "destructive"),
    )
    async def nb_ui_console_inject(
        server_name: str,
        code: str,
        path: str | None = None,
        activate: bool = True,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        args: dict[str, Any] = {"code": code, "activate": activate}
        if path is not None:
            args["path"] = path
        return await _call_upstream(ctx, server_name, "console_inject", args)

    @mcp.tool(
        name=_ui_name("docmanager_open"),
        description=_description(
            "Open (or reveal) a file in a JupyterLab tab, making it the active"
            " document."
        ),
        annotations=_annotations("Open document", "mutating"),
    )
    async def nb_ui_docmanager_open(
        server_name: str,
        path: str,
        factory: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        args: dict[str, Any] = {"path": path}
        if factory is not None:
            args["factory"] = factory
        return await _call_upstream(ctx, server_name, "docmanager_open", args)

    @mcp.tool(
        name=_ui_name("docmanager_new-untitled"),
        description=_description(
            "Create a new untitled notebook, file, or folder under path."
        ),
        annotations=_annotations("New untitled", "mutating"),
    )
    async def nb_ui_docmanager_new_untitled(
        server_name: str,
        content_type: Literal["notebook", "file", "directory"],
        path: str = "",
        ext: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        args: dict[str, Any] = {"type": content_type, "path": path}
        if ext is not None:
            args["ext"] = ext
        return await _call_upstream(ctx, server_name, "docmanager_new-untitled", args)

    @mcp.tool(
        name=_ui_name("filebrowser_go-to-path"),
        description=_description(
            "Navigate the file browser to path (selecting it if it is a file)."
        ),
        annotations=_annotations("Go to path", "read_only"),
    )
    async def nb_ui_filebrowser_go_to_path(
        server_name: str,
        path: str,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        return await _call_upstream(
            ctx, server_name, "filebrowser_go-to-path", {"path": path}
        )

    @mcp.tool(
        name=_ui_name("apputils_change-theme"),
        description=_description(
            "Switch the JupyterLab theme (e.g. 'JupyterLab Light', 'JupyterLab Dark')."
        ),
        annotations=_annotations("Change theme", "read_only"),
    )
    async def nb_ui_apputils_change_theme(
        server_name: str,
        theme: str,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        return await _call_upstream(
            ctx, server_name, "apputils_change-theme", {"theme": theme}
        )

    @mcp.tool(
        name=_ui_name("documentsearch_start"),
        description=_description(
            "Open the find bar in the active document, optionally pre-filled"
            " with search_text."
        ),
        annotations=_annotations("Find", "read_only"),
    )
    async def nb_ui_documentsearch_start(
        server_name: str,
        search_text: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, NbProxyResult]:
        args: dict[str, Any] = {}
        if search_text is not None:
            args["searchText"] = search_text
        return await _call_upstream(ctx, server_name, "documentsearch_start", args)
