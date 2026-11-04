"""The seven server-lifecycle tools: create/list/get/stop/delete, images, sites.

Owner scoping is strict: the owner of every server is always the hub user
the caller's own token belongs to (``get_hub_identity``) -- no tool here
takes an owner/username argument, and every hub call is made with the
caller's token, so the hub itself enforces that a user can only see and
change their own servers.

Servers launched from the BinderHub UI are ordinary named servers of the
same user, so they show up here (and work with the ``nb_*`` tools) too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from mcp.server.mcpserver import Context  # noqa: TC002
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from af_jupyterhub_mcp.auth.hub import get_hub_identity
from af_jupyterhub_mcp.hub.errors import (
    GuardrailError,
    HubAPIError,
    NameConflictError,
    NotFoundOrNotYoursError,
    SpawnFailedError,
    SpawnTimeoutError,
)
from af_jupyterhub_mcp.hub.servers import (
    build_user_options,
    check_server_name,
    find_server,
    follow_spawn,
    resolve_server_name,
    summarize_server,
    validate_create_request,
)
from af_jupyterhub_mcp.tools._helpers import (
    append_next_actions,
    format_error,
    format_server,
    format_server_list,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from af_jupyterhub_mcp.config import Settings
    from af_jupyterhub_mcp.hub.client import HubClient

_LIST_HINT = "Use `list_jupyter_servers` to see your own servers."
_RELINK_HINT = "Re-link your JupyterHub account in af-mcp-platform, then retry."


class ServerSummary(BaseModel):
    """The curated server fields shown in the Field | Value markdown table.

    Mirrors ``hub.servers.summarize_server``: resource values are the
    strings the hub stored in ``user_options``, and ``url`` is the server's
    JupyterLab URL on the public hub. No token is ever part of this view.
    """

    name: str
    status: str
    image: str | None = None
    site: str | None = None
    cpu: str | None = None
    memory_gb: str | None = None
    qos: str | None = None
    gpus: str | None = None
    gpu_product: str | None = None
    binder_request: str | None = None
    url: str | None = None
    started: str | None = None
    last_activity: str | None = None


class CreateJupyterServerResult(BaseModel):
    """Structured result of create_jupyter_server."""

    server: ServerSummary


class GetJupyterServerResult(BaseModel):
    """Structured result of get_jupyter_server."""

    server: ServerSummary


class ListJupyterServersResult(BaseModel):
    """Structured result of list_jupyter_servers."""

    servers: list[ServerSummary]


class StopJupyterServerResult(BaseModel):
    """Structured result of stop_jupyter_server."""

    name: str
    stopped: bool = True


class DeleteJupyterServerResult(BaseModel):
    """Structured result of delete_jupyter_server."""

    name: str
    deleted: bool = True


class ListSupportedImagesResult(BaseModel):
    """Structured result of list_supported_images."""

    cpu_images: list[str]
    gpu_images: list[str]


class ListSitesResult(BaseModel):
    """Structured result of list_sites."""

    sites: list[str]


def _lifespan(ctx: Any) -> tuple[HubClient, Settings]:
    """Return (hub client, settings) from the lifespan context."""
    lc = ctx.request_context.lifespan_context
    return lc["hub"], lc["settings"]


def _hub_error_hints(exc: HubAPIError) -> list[str]:
    if exc.status in {401, 403}:
        return [_RELINK_HINT]
    if exc.status == 404:
        return [_LIST_HINT]
    return []


async def _summary_of(
    hub: HubClient, settings: Settings, token: str, owner: str, name: str
) -> dict[str, Any]:
    user = await hub.get_user(token, owner)
    model = find_server(user, name)
    if model is None:
        msg = f"no server named {name!r} (or it is not yours)"
        raise NotFoundOrNotYoursError(msg)
    return summarize_server(model, settings.public_url)


def register(mcp: MCPServer) -> None:
    """Register the seven server-lifecycle tools on *mcp*."""

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Create JupyterHub server",
            read_only_hint=False,
        )
    )
    async def create_jupyter_server(
        image: str,
        name: str | None = None,
        site: str = "local",
        cpu_cores: int = 1,
        memory_gb: int = 1,
        guaranteed_qos: bool = False,
        gpus: int = 0,
        gpu_product: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, CreateJupyterServerResult]:
        """Start one of the caller's own JupyterHub named servers and wait until it is ready.

        image must be on the allowlist (see list_supported_images): a GPU
        image when gpus > 0, a CPU image otherwise. site is one of
        list_sites. name is optional; omitted, the first free notebook-<n>
        is used. Giving the name of one of your stopped servers starts it
        again with these settings. guaranteed_qos reserves cpu_cores and
        memory_gb exactly (requests = limits) instead of a burstable
        allocation. gpu_product pins a GPU model; omitted, any model is
        accepted. Progress is reported while the hub spawns the server; the
        result includes the server's JupyterLab URL. Each user can have at
        most 10 named servers, stopped ones included.
        """
        try:
            hub, settings = _lifespan(ctx)
            identity = await get_hub_identity(ctx)
            validate_create_request(
                image=image,
                site=site,
                cpu_cores=cpu_cores,
                memory_gb=memory_gb,
                gpus=gpus,
                gpu_product=gpu_product,
                settings=settings,
            )
            user = await hub.get_user(identity.token, identity.name)
            name = resolve_server_name(user, name)

            options = build_user_options(
                image=image,
                site=site,
                cpu_cores=cpu_cores,
                memory_gb=memory_gb,
                guaranteed_qos=guaranteed_qos,
                gpus=gpus,
                gpu_product=gpu_product,
            )
            status = await hub.start_server(
                identity.token, identity.name, name, options
            )
            if status == 202:

                async def _report(event: dict[str, Any]) -> None:
                    message = event.get("message")
                    await ctx.report_progress(
                        progress=float(event.get("progress") or 0),
                        total=100.0,
                        message=message if isinstance(message, str) else None,
                    )

                await follow_spawn(
                    hub,
                    identity.token,
                    identity.name,
                    name,
                    timeout=settings.launch_timeout_seconds,
                    on_event=_report,
                )
            info = await _summary_of(hub, settings, identity.token, identity.name, name)
        except GuardrailError as exc:
            return format_error(
                exc,
                hints=[
                    "Use `list_supported_images` for the allowed images.",
                    "Use `list_sites` for the allowed sites.",
                ],
            )
        except NameConflictError as exc:
            return format_error(
                exc,
                hints=[
                    "Pick another name, or omit name to get a free one.",
                    "Use `stop_jupyter_server` first to restart it with new settings.",
                ],
            )
        except SpawnFailedError as exc:
            return format_error(
                exc,
                hints=[
                    f"Use `get_jupyter_server(name={name!r})` to inspect it.",
                    f"Use `delete_jupyter_server(name={name!r})` to free the entry.",
                ],
            )
        except SpawnTimeoutError as exc:
            return format_error(
                exc,
                context="The hub keeps spawning it; it was not deleted.",
                hints=[
                    f"Use `get_jupyter_server(name={name!r})` to check on it.",
                    f"Use `delete_jupyter_server(name={name!r})` to cancel it.",
                ],
            )
        except HubAPIError as exc:
            hints = _hub_error_hints(exc)
            if exc.status == 400:
                hints = [
                    (
                        "Each user can have at most 10 named servers, stopped ones"
                        " included; use `delete_jupyter_server` to free one."
                    ),
                    _LIST_HINT,
                ]
            return format_error(exc, hints=hints)
        except PermissionError as exc:
            return format_error(exc, hints=[_RELINK_HINT])
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        text = append_next_actions(
            format_server(info),
            [
                f"Use the `nb_*` tools with `server_name={name!r}` to work in it.",
                (
                    f"Open {info['url']} in a browser for the JupyterLab UI"
                    " (needed by the `nb_ui_*` tools)."
                ),
            ],
        )
        payload = CreateJupyterServerResult(server=ServerSummary(**info))
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=payload.model_dump(mode="json"),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List your JupyterHub servers",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def list_jupyter_servers(
        *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, ListJupyterServersResult]:
        """List the caller's own JupyterHub named servers, stopped ones included.

        Includes servers launched from the BinderHub UI. Never includes tokens.
        """
        try:
            hub, settings = _lifespan(ctx)
            identity = await get_hub_identity(ctx)
            user = await hub.get_user(identity.token, identity.name)
            infos = [
                summarize_server(model, settings.public_url)
                for name, model in sorted((user.get("servers") or {}).items())
                if name and isinstance(model, dict)
            ]
        except HubAPIError as exc:
            return format_error(exc, hints=_hub_error_hints(exc))
        except PermissionError as exc:
            return format_error(exc, hints=[_RELINK_HINT])
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        payload = ListJupyterServersResult(
            servers=[ServerSummary(**info) for info in infos]
        )
        return CallToolResult(
            content=[TextContent(type="text", text=format_server_list(infos))],
            structured_content=payload.model_dump(mode="json"),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get JupyterHub server status",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def get_jupyter_server(
        name: str, *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, GetJupyterServerResult]:
        """Get status, image, resources, and URL of one of the caller's own servers.

        Refuses (with a not-found-or-not-yours message) for any name that is
        not one of the caller's servers. Never includes tokens.
        """
        try:
            hub, settings = _lifespan(ctx)
            check_server_name(name)
            identity = await get_hub_identity(ctx)
            info = await _summary_of(hub, settings, identity.token, identity.name, name)
        except (NotFoundOrNotYoursError, GuardrailError) as exc:
            return format_error(exc, hints=[_LIST_HINT])
        except HubAPIError as exc:
            return format_error(exc, hints=_hub_error_hints(exc))
        except PermissionError as exc:
            return format_error(exc, hints=[_RELINK_HINT])
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        payload = GetJupyterServerResult(server=ServerSummary(**info))
        return CallToolResult(
            content=[TextContent(type="text", text=format_server(info))],
            structured_content=payload.model_dump(mode="json"),
        )

    async def _stop(ctx: Any, name: str, *, remove: bool) -> int:
        hub, _settings = _lifespan(ctx)
        check_server_name(name)
        identity = await get_hub_identity(ctx)
        try:
            return await hub.stop_server(
                identity.token, identity.name, name, remove=remove
            )
        except HubAPIError as exc:
            if exc.status == 404:
                msg = f"no server named {name!r} (or it is not yours)"
                raise NotFoundOrNotYoursError(msg) from exc
            raise

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Stop JupyterHub server",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def stop_jupyter_server(
        name: str, *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, StopJupyterServerResult]:
        """Stop one of the caller's own servers, but keep its named-server entry.

        Running kernels and unsaved notebook state are lost; files in the
        home directory stay. The stopped entry still counts toward the
        per-user limit of 10 named servers and can be started again with
        create_jupyter_server(name=...). Use delete_jupyter_server to stop it
        and remove the entry.
        """
        # The try/except/else split (rather than a trailing return inside the
        # try block) is deliberate -- ruff's TRY300 flags a return-inside-try
        # as it can silently mask an exception raised by the return expression
        # itself; pylint's no-else-return disagrees, so it is disabled here.
        try:  # pylint: disable=no-else-return
            status = await _stop(ctx, name, remove=False)
        except (NotFoundOrNotYoursError, GuardrailError) as exc:
            return format_error(exc, hints=[_LIST_HINT])
        except HubAPIError as exc:
            return format_error(exc, hints=_hub_error_hints(exc))
        except PermissionError as exc:
            return format_error(exc, hints=[_RELINK_HINT])
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        else:
            done = "Stopped" if status == 204 else "Stopping"
            text = append_next_actions(
                f"{done} JupyterHub server {name!r}; its entry is kept.",
                [
                    f"Use `create_jupyter_server(name={name!r}, ...)` to start it again.",
                    f"Use `delete_jupyter_server(name={name!r})` to remove the entry.",
                ],
            )
            payload = StopJupyterServerResult(name=name)
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structured_content=payload.model_dump(mode="json"),
            )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Delete JupyterHub server",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def delete_jupyter_server(
        name: str, *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, DeleteJupyterServerResult]:
        """Stop one of the caller's own servers and remove its named-server entry.

        Frees one of the 10 named-server slots. Running kernels and unsaved
        notebook state are lost; files in the home directory stay.
        """
        try:  # pylint: disable=no-else-return
            await _stop(ctx, name, remove=True)
        except (NotFoundOrNotYoursError, GuardrailError) as exc:
            return format_error(exc, hints=[_LIST_HINT])
        except HubAPIError as exc:
            return format_error(exc, hints=_hub_error_hints(exc))
        except PermissionError as exc:
            return format_error(exc, hints=[_RELINK_HINT])
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        else:
            payload = DeleteJupyterServerResult(name=name)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text", text=f"Deleted JupyterHub server {name!r}."
                    )
                ],
                structured_content=payload.model_dump(mode="json"),
            )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List supported images",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def list_supported_images(
        *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, ListSupportedImagesResult]:
        """List the CPU and GPU images allowed by create_jupyter_server, from chart values."""
        try:
            _hub, settings = _lifespan(ctx)
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        lines = ["**CPU images** (gpus=0):"]
        lines.extend(f"- {i}" for i in settings.cpu_images)
        lines.append("\n**GPU images** (gpus>0):")
        lines.extend(f"- {i}" for i in settings.gpu_images)
        payload = ListSupportedImagesResult(
            cpu_images=list(settings.cpu_images),
            gpu_images=list(settings.gpu_images),
        )
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=payload.model_dump(mode="json"),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List sites",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def list_sites(
        *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, ListSitesResult]:
        """List the site names create_jupyter_server accepts, from chart values."""
        try:
            _hub, settings = _lifespan(ctx)
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        lines = ["**Sites:**"]
        lines.extend(f"- {s}" for s in settings.sites)
        payload = ListSitesResult(sites=list(settings.sites))
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=payload.model_dump(mode="json"),
        )
