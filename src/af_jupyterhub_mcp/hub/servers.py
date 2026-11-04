"""Named-server domain logic: names, validation, user_options, summaries, spawns.

A server's launch request is its JupyterHub ``user_options``, the same shape
the AF hub spawner (``binderspawner_mixin.py``) reads when the BinderHub form
launches one::

    {"image": "<image>",
     "resource_requests": {"site": "<site>", "cpu": "<n>", "memory": "<GB>",
                           "qos": "Guaranteed" | "Burstable",
                           "gpuCount": "<n>", "gpuModel": "<product>"}}

Values are strings, as the form sends them. ``gpuModel`` is omitted to accept
any GPU product: the spawner turns any value it is given (other than the
form's ``"undefined"``) into a ``nvidia.com/gpu.product`` node selector.
``qos`` other than ``"Guaranteed"`` leaves the spawner's default (burstable)
requests and limits in place.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import anyio

from af_jupyterhub_mcp.config import (
    CPU_CORES_MAX,
    CPU_CORES_MIN,
    MEMORY_GB_MAX,
    MEMORY_GB_MIN,
    Settings,
)
from af_jupyterhub_mcp.hub.errors import (
    GuardrailError,
    ImageNotAllowedError,
    NameConflictError,
    SpawnFailedError,
    SpawnTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection

    from af_jupyterhub_mcp.hub.client import HubClient

SERVER_NAME_PREFIX = "notebook"
_MAX_GENERATE_ATTEMPTS = 100

# Names this backend creates: a DNS label (lowercase alphanumerics and '-',
# 1-63 characters, alphanumeric at both ends). Stricter than the hub, which
# only forbids '/', so the name is also safe in pod names and URLs.
_NEW_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_new_server_name(name: str) -> None:
    """Raise GuardrailError unless *name* is a valid name for a new server."""
    if not _NEW_NAME.match(name):
        msg = (
            f"server name {name!r} is invalid: use 1-63 lowercase letters, "
            "digits, or '-', starting and ending with a letter or digit"
        )
        raise GuardrailError(msg)


def check_server_name(name: str) -> None:
    """Raise GuardrailError unless the hub can address an existing server *name*.

    Existing servers may have been created by other clients (the BinderHub
    UI names them ``<org>-<repo>-<random>``, underscores included), so only
    the hub's own rule applies here: non-empty (the default server is not
    managed by this backend) and no '/'.
    """
    if not name or "/" in name:
        msg = (
            f"server name {name!r} is invalid: it must be non-empty and contain no '/'"
        )
        raise GuardrailError(msg)


def generate_server_name(existing: Collection[str]) -> str | None:
    """Return the first free ``notebook-<n>`` name, or None if none is free."""
    for i in range(1, _MAX_GENERATE_ATTEMPTS):
        candidate = f"{SERVER_NAME_PREFIX}-{i}"
        if candidate not in existing:
            return candidate
    return None


def resolve_server_name(user_model: dict[str, Any], name: str | None) -> str:
    """Return the server name a create request should use.

    With no *name*, the first free ``notebook-<n>``. A given *name* must be a
    valid new name; it may be one of the caller's stopped servers (the hub
    starts that entry again) but not an active one.

    Raises:
        GuardrailError: *name* is invalid, or no generated name is free.
        NameConflictError: *name* is one of the caller's active servers.
    """
    if name is None:
        generated = generate_server_name(set(user_model.get("servers") or {}))
        if generated is None:
            msg = "no free notebook-<n> server name; pass name explicitly"
            raise GuardrailError(msg)
        return generated
    validate_new_server_name(name)
    model = find_server(user_model, name)
    if model is not None and not model.get("stopped"):
        msg = f"server {name!r} already exists and is not stopped"
        raise NameConflictError(msg)
    return name


def validate_create_request(
    *,
    image: str,
    site: str,
    cpu_cores: int,
    memory_gb: int,
    gpus: int,
    gpu_product: str | None,
    settings: Settings,
) -> None:
    """Validate a create_jupyter_server request against server-side guardrails.

    Raises:
        GuardrailError: A range, site, or GPU-product rule was violated.
        ImageNotAllowedError: The image is not on the allowlist matching the
            request (GPU images for ``gpus > 0``, CPU images otherwise).
    """
    if not CPU_CORES_MIN <= cpu_cores <= CPU_CORES_MAX:
        msg = (
            f"cpu_cores={cpu_cores} is out of range [{CPU_CORES_MIN}, {CPU_CORES_MAX}]"
        )
        raise GuardrailError(msg)
    if not MEMORY_GB_MIN <= memory_gb <= MEMORY_GB_MAX:
        msg = (
            f"memory_gb={memory_gb} is out of range [{MEMORY_GB_MIN}, {MEMORY_GB_MAX}]"
        )
        raise GuardrailError(msg)
    if gpus < 0:
        msg = f"gpus={gpus} must be >= 0"
        raise GuardrailError(msg)
    if gpu_product is not None and gpus == 0:
        msg = "gpu_product is only meaningful with gpus > 0"
        raise GuardrailError(msg)
    if site not in settings.sites:
        msg = f"site {site!r} is not one of the configured sites: {', '.join(settings.sites)}"
        raise GuardrailError(msg)

    if image not in settings.all_images:
        msg = (
            f"image {image!r} is not on the supported-images allowlist "
            "(see list_supported_images)"
        )
        raise ImageNotAllowedError(msg)
    if gpus > 0 and image not in settings.gpu_images:
        msg = f"gpus={gpus} needs a GPU image; {image!r} is not on the GPU list"
        raise ImageNotAllowedError(msg)
    if gpus == 0 and image not in settings.cpu_images:
        msg = f"gpus=0 needs a CPU image; {image!r} is not on the CPU list"
        raise ImageNotAllowedError(msg)


def build_user_options(
    *,
    image: str,
    site: str,
    cpu_cores: int,
    memory_gb: int,
    guaranteed_qos: bool,
    gpus: int,
    gpu_product: str | None,
) -> dict[str, Any]:
    """Return the spawner ``user_options`` for a launch (see the module docstring)."""
    resource_requests: dict[str, str] = {
        "site": site,
        "cpu": str(cpu_cores),
        "memory": str(memory_gb),
        "qos": "Guaranteed" if guaranteed_qos else "Burstable",
        "gpuCount": str(gpus),
    }
    if gpu_product is not None:
        resource_requests["gpuModel"] = gpu_product
    return {"image": image, "resource_requests": resource_requests}


def server_status(model: dict[str, Any]) -> str:
    """Return ``ready``, ``pending <action>``, or ``stopped`` for a server model."""
    if model.get("pending"):
        return f"pending {model['pending']}"
    if model.get("ready"):
        return "ready"
    return "stopped"


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def summarize_server(model: dict[str, Any], hub_url: str) -> dict[str, Any]:
    """Return the curated, token-free view of a hub server model.

    Only the fields listed here are copied out of ``user_options``; anything
    else (notably the ``token`` BinderHub stores there) never leaves this
    function.
    """
    options = model.get("user_options") or {}
    requests = options.get("resource_requests") or {}
    summary: dict[str, Any] = {
        "name": model.get("name"),
        "status": server_status(model),
        "image": _str_or_none(options.get("image")),
        "site": _str_or_none(requests.get("site")),
        "cpu": _str_or_none(requests.get("cpu")),
        "memory_gb": _str_or_none(requests.get("memory")),
        "qos": _str_or_none(requests.get("qos")),
        "gpus": _str_or_none(requests.get("gpuCount")),
        "gpu_product": _str_or_none(requests.get("gpuModel")),
        "url": f"{hub_url}{model['url']}" if model.get("url") else None,
        "started": model.get("started"),
        "last_activity": model.get("last_activity"),
    }
    if options.get("binder_request"):
        summary["binder_request"] = str(options["binder_request"])
    return summary


def find_server(user_model: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Return the caller's server model named *name*, or None."""
    servers = user_model.get("servers") or {}
    model = servers.get(name)
    return model if isinstance(model, dict) else None


async def _ignore_event(_event: dict[str, Any]) -> None:
    return None


async def follow_spawn(
    hub: HubClient,
    token: str,
    name: str,
    server: str,
    *,
    timeout: float,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] = _ignore_event,
) -> dict[str, Any]:
    """Follow *server*'s progress stream until it is ready; return the ready event.

    Every event is passed to *on_event* first (tools forward them as MCP
    progress notifications).

    Raises:
        SpawnFailedError: the hub sent a ``failed`` event, or closed the
            stream without a ``ready`` one.
        SpawnTimeoutError: *timeout* seconds passed first. The spawn keeps
            going on the hub; the server is left pending, not deleted.
    """
    try:
        with anyio.fail_after(timeout):
            async for event in hub.progress_events(token, name, server):
                await on_event(event)
                if event.get("ready"):
                    return event
                if event.get("failed"):
                    message = event.get("message") or "Spawn failed"
                    raise SpawnFailedError(str(message))
    except TimeoutError as exc:
        msg = f"server {server!r} was not ready after {timeout} seconds"
        raise SpawnTimeoutError(msg) from exc
    msg = f"server {server!r} failed to start (progress stream ended before ready)"
    raise SpawnFailedError(msg)
