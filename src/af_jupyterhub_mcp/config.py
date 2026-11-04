"""Environment-driven configuration for af-jupyterhub-mcp.

Every knob here has a chart-values-driven env var so an operator can change
the hub URLs, the image allowlist, the site list, or per-image tool support
without a code release (see ``charts/af-jupyterhub-mcp/values.yaml`` ->
``templates/deployment.yaml``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# Server-side guardrail bounds for create_jupyter_server. These are
# validation, not quota, and are ALWAYS enforced; they match
# af-jupyterlab-mcp's ranges so both backends accept the same requests.
CPU_CORES_MIN = 1
CPU_CORES_MAX = 16
MEMORY_GB_MIN = 1
MEMORY_GB_MAX = 256

DEFAULT_CPU_IMAGES = ("hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest",)
DEFAULT_GPU_IMAGES = ("hub.opensciencegrid.org/usatlas/ml-platform-gpu:latest",)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _parse_image_tool_overrides(name: str, raw: str) -> dict[str, frozenset[str]]:
    """Parse ``{"<image>": ["<upstream tool id>", ...]}`` JSON from env var *name*."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"{name} is not valid JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{name} must be a JSON object mapping image -> list of tool ids"
        # ValueError (not TypeError): every malformed env value is a config error.
        raise ValueError(msg)  # noqa: TRY004
    overrides: dict[str, frozenset[str]] = {}
    for image, tools in data.items():
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            msg = f"{name}[{image!r}] must be a list of upstream tool id strings"
            raise ValueError(msg)
        overrides[image] = frozenset(tools)
    return overrides


def _env_positive_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{name} must be a positive integer, got {raw!r}"
        raise ValueError(msg) from exc
    if value <= 0:
        msg = f"{name} must be a positive integer, got {raw!r}"
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, normally built once in the server lifespan."""

    # JupyterHub base URL this backend connects to. Every hub API call,
    # launch, and notebook proxy call (``{hub_url}/user/<name>/<server>/mcp``)
    # goes through it. In-cluster this is the hub's proxy Service (e.g.
    # http://proxy-public.binderhub.svc.cluster.local).
    hub_url: str = "https://jupyterhub.af.uchicago.edu"

    # JupyterHub base URL shown to users (server lab URLs in tool results).
    # None means links use hub_url; see the ``public_url`` property.
    public_hub_url: str | None = None

    # Image allowlist, chart-values-driven. create_jupyter_server requires a
    # cpu_images entry for gpus=0 and a gpu_images entry for gpus>0. Every
    # image must ship jupyterhub-singleuser so the hub can start it.
    cpu_images: tuple[str, ...] = DEFAULT_CPU_IMAGES
    gpu_images: tuple[str, ...] = DEFAULT_GPU_IMAGES

    # Per-image tool support, chart-values-driven. Maps an exact image string
    # (a server's ``user_options.image``) to the upstream jupyter-mcp-server
    # tool ids (e.g. ``read_cell``, ``notebook_run-all-cells``) that image's
    # notebook server offers. Images without an entry are assumed to support
    # every nb_*/nb_ui_* tool, i.e. to match the latest ml-platform image; an
    # empty set means none at all.
    image_tool_overrides: Mapping[str, frozenset[str]] = field(default_factory=dict)

    # Site names create_jupyter_server accepts. The hub spawner places a
    # server on the named site and falls back to the local site for any name
    # it does not recognise, so "local" always works.
    sites: tuple[str, ...] = ("local",)

    # How long create_jupyter_server follows the hub's spawn progress stream
    # before giving up (the server is left pending, not deleted).
    launch_timeout_seconds: int = 900

    # How long a verified hub token's identity is cached, keyed by its
    # SHA-256, so each MCP request does not cost a /hub/api/user round trip.
    auth_cache_ttl_seconds: int = 60

    @property
    def public_url(self) -> str:
        """Return the hub base URL for user-facing links."""
        return self.public_hub_url or self.hub_url

    @property
    def all_images(self) -> tuple[str, ...]:
        """Return the deduplicated union of the CPU and GPU image allowlists."""
        seen: dict[str, None] = {}
        for image in (*self.cpu_images, *self.gpu_images):
            seen.setdefault(image, None)
        return tuple(seen)

    @classmethod
    def from_env(cls) -> Settings:
        """Build Settings from the JUPYTERHUB_MCP_* environment variables."""
        kwargs: dict[str, object] = {}
        if hub_url := os.environ.get("JUPYTERHUB_MCP_HUB_URL"):
            kwargs["hub_url"] = hub_url.rstrip("/")
        if public_hub_url := os.environ.get("JUPYTERHUB_MCP_PUBLIC_HUB_URL"):
            kwargs["public_hub_url"] = public_hub_url.rstrip("/")
        if cpu_images := os.environ.get("JUPYTERHUB_MCP_CPU_IMAGES"):
            kwargs["cpu_images"] = _split_csv(cpu_images)
        if gpu_images := os.environ.get("JUPYTERHUB_MCP_GPU_IMAGES"):
            kwargs["gpu_images"] = _split_csv(gpu_images)
        if overrides := os.environ.get("JUPYTERHUB_MCP_IMAGE_TOOL_OVERRIDES"):
            kwargs["image_tool_overrides"] = _parse_image_tool_overrides(
                "JUPYTERHUB_MCP_IMAGE_TOOL_OVERRIDES", overrides
            )
        if sites := os.environ.get("JUPYTERHUB_MCP_SITES"):
            kwargs["sites"] = _split_csv(sites)
        timeout = _env_positive_int("JUPYTERHUB_MCP_LAUNCH_TIMEOUT_SECONDS")
        if timeout is not None:
            kwargs["launch_timeout_seconds"] = timeout
        ttl = _env_positive_int("JUPYTERHUB_MCP_AUTH_CACHE_TTL_SECONDS")
        if ttl is not None:
            kwargs["auth_cache_ttl_seconds"] = ttl
        return cls(**kwargs)  # type: ignore[arg-type]
