"""Tests for GpuResourcesClient (the BinderHub /resources client)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from af_jupyterhub_mcp.hub.errors import ResourcesError
from af_jupyterhub_mcp.resources import GpuResourcesClient

if TYPE_CHECKING:
    from collections.abc import Callable

URL = "https://binder.example.org/resources"


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> GpuResourcesClient:
    return GpuResourcesClient(URL, transport=httpx2.MockTransport(handler))


async def test_returns_the_resources_list() -> None:
    sample = [
        {"product": "NVIDIA-A40", "memory": 46068, "count": 2, "available": 2},
    ]

    def handle(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == URL
        assert request.method == "GET"
        # Cluster-wide endpoint: the caller's hub token is never forwarded.
        assert "authorization" not in request.headers
        return httpx2.Response(200, json={"resources": sample})

    client = _client(handle)
    try:
        assert await client.get_gpu_resources() == sample
    finally:
        await client.aclose()


async def test_non_dict_entries_are_dropped() -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"resources": [{"product": "A"}, "junk", 3]})

    client = _client(handle)
    try:
        assert await client.get_gpu_resources() == [{"product": "A"}]
    finally:
        await client.aclose()


async def test_error_status_raises_resources_error() -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, text="down")

    client = _client(handle)
    try:
        with pytest.raises(ResourcesError, match="503"):
            await client.get_gpu_resources()
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "body",
    [
        {"not_resources": []},
        {"resources": "oops"},
        [1, 2, 3],
    ],
)
async def test_malformed_body_raises_resources_error(body: object) -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=body)

    client = _client(handle)
    try:
        with pytest.raises(ResourcesError, match=r"malformed|resources"):
            await client.get_gpu_resources()
    finally:
        await client.aclose()


async def test_unreachable_endpoint_raises_resources_error() -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        msg = "boom"
        raise httpx2.ConnectError(msg)

    client = _client(handle)
    try:
        with pytest.raises(ResourcesError, match="could not reach"):
            await client.get_gpu_resources()
    finally:
        await client.aclose()
