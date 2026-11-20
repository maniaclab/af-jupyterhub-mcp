"""Client for the BinderHub GPU resources endpoint.

``create_jupyter_server`` launches a GPU server by turning ``gpu_product`` into
a ``nvidia.com/gpu.product`` node selector, but which GPU products actually
exist on the local AF site -- and how many of each are free right now -- is
dynamic. The AF BinderHub publishes it at its ``/resources`` endpoint (the same
data its launch page shows), shaped as::

    {"resources": [
        {"product": "<nvidia.com/gpu.product>", "memory": <MB>,
         "count": <total>, "total_requests": <in use>, "available": <free>},
        ...
    ]}

The view is unauthenticated, read-only, and cluster-wide (not per user), so no
hub token is sent. It reflects the **local** site only; remote sites (``ssl``,
``nrp``) are not covered. The URL is chart-values-driven
(``JUPYTERHUB_MCP_GPU_RESOURCES_URL``); in-cluster, point it at the binder
Service, e.g. ``http://binderhub.binderhub.svc.cluster.local/resources``.

This lives outside ``hub/`` on purpose: it talks to BinderHub, not the
JupyterHub REST API, and never touches the caller's token. Like ``HubClient``
it wraps one shared ``httpx2`` client built in ``server.make_app`` (tests inject
a ``MockTransport``) and is closed by the lifespan.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2

from af_jupyterhub_mcp.hub.errors import ResourcesError

# The endpoint lists nodes and their pods to compute availability; give it a
# little more than a trivial timeout, but it is not a long-poll like a spawn.
_TIMEOUT = httpx2.Timeout(30.0)


class GpuResourcesClient:
    """Fetches the BinderHub GPU resources endpoint; see the module docstring."""

    def __init__(
        self,
        url: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self._client = httpx2.AsyncClient(timeout=_TIMEOUT, transport=transport)

    async def aclose(self) -> None:
        """Close the shared HTTP client."""
        await self._client.aclose()

    async def get_gpu_resources(self) -> list[dict[str, Any]]:
        """Return the endpoint's ``resources`` list (GPU product availability dicts).

        Raises:
            ResourcesError: the endpoint could not be reached, answered with an
                error status, or sent a body that is not
                ``{"resources": [ {...}, ... ]}``.
        """
        try:
            response = await self._client.get(self.url)
        except httpx2.HTTPError as exc:
            msg = (
                "could not reach the BinderHub resources endpoint: "
                f"{type(exc).__name__}: {exc}"
            )
            raise ResourcesError(msg) from exc
        if response.is_error:
            msg = f"BinderHub resources endpoint returned {response.status_code}"
            raise ResourcesError(msg)
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = f"malformed response from the BinderHub resources endpoint: {exc}"
            raise ResourcesError(msg) from exc
        resources = body.get("resources") if isinstance(body, dict) else None
        if not isinstance(resources, list):
            msg = (
                "malformed response from the BinderHub resources endpoint: "
                "expected a 'resources' list"
            )
            raise ResourcesError(msg)
        return [item for item in resources if isinstance(item, dict)]
