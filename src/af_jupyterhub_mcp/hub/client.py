"""Async client for the JupyterHub REST API and the hub proxy.

Every JupyterHub call this backend makes goes through ``HubClient``, which
wraps one shared ``httpx2.AsyncClient`` (``httpx2`` is the HTTP client the mcp
SDK itself uses, so the notebook proxy calls and these share one stack). The
caller's own hub token is passed to every method and sent as
``Authorization: token <hub token>``; the client holds no credentials of its
own and never logs or returns the token.

Endpoints (JupyterHub 5.2, ``jupyterhub/apihandlers/users.py``):

- ``GET  /hub/api/user`` -- the token owner's model, with expanded ``scopes``
- ``GET  /hub/api/users/<name>?include_stopped_servers=1`` -- every named
  server, including stopped ones (without the flag the hub lists only active
  servers, which would hide entries that still count toward the per-user
  named-server limit)
- ``POST /hub/api/users/<name>/servers/<server>`` -- start, body is the
  spawner's ``user_options``; 201 started, 202 pending
- ``GET  /hub/api/users/<name>/servers/<server>/progress`` -- event stream
  of ``data: {json}`` lines (``SpawnProgressAPIHandler``)
- ``DELETE /hub/api/users/<name>/servers/<server>`` -- stop; with
  ``{"remove": true}`` the named-server entry is deleted too
"""

from __future__ import annotations

import json
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx2

from af_jupyterhub_mcp.hub.errors import HubAPIError, HubRequestError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Connect/write/pool timeout for hub API calls. The progress stream's read
# timeout only needs to outlast the hub's 8 s keepalive interval.
_HUB_TIMEOUT = httpx2.Timeout(30.0)
# Notebook MCP calls can hold a response open while a cell executes; match
# the mcp SDK's own default (30 s, with a 300 s read timeout).
_NOTEBOOK_TIMEOUT = httpx2.Timeout(30.0, read=300.0)


def _quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"token {token}"}


def _error_from_response(response: httpx2.Response) -> HubAPIError:
    """Build a HubAPIError carrying the hub's own ``message`` text when present."""
    message = ""
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        message = body["message"].strip()
    if not message:
        try:
            message = HTTPStatus(response.status_code).phrase
        except ValueError:
            message = "Unknown Error"
    return HubAPIError(
        response.status_code, f"JupyterHub returned {response.status_code}: {message}"
    )


async def parse_progress_lines(
    lines: AsyncIterator[str],
) -> AsyncIterator[dict[str, Any]]:
    """Yield the JSON object of every ``data:`` line in a hub progress stream.

    The hub writes each event as a single ``data: {json}`` line followed by a
    blank line, and sends bare blank lines as keepalives
    (``SpawnProgressAPIHandler.keepalive``); blank lines, ``:`` comments, and
    other SSE fields are skipped. A ``data:`` payload that is not a JSON
    object raises ``HubAPIError`` rather than being silently dropped.
    """
    async for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            msg = f"malformed progress event from JupyterHub: {exc}"
            raise HubAPIError(None, msg) from exc
        if not isinstance(event, dict):
            msg = "malformed progress event from JupyterHub: not a JSON object"
            raise HubAPIError(None, msg)
        yield event


class HubClient:
    """JupyterHub REST API client; see the module docstring."""

    def __init__(
        self,
        hub_url: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.hub_url = hub_url.rstrip("/")
        # Kept so notebook_http_client can route through the same transport
        # (tests inject an httpx2.MockTransport; production leaves it None).
        self._transport = transport
        self._client = httpx2.AsyncClient(
            base_url=self.hub_url, timeout=_HUB_TIMEOUT, transport=transport
        )

    async def aclose(self) -> None:
        """Close the shared HTTP client."""
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx2.Response:
        try:
            response = await self._client.request(
                method, path, headers=_auth(token), params=params, json=body
            )
        except httpx2.HTTPError as exc:
            msg = f"could not reach JupyterHub: {type(exc).__name__}: {exc}"
            raise HubRequestError(msg) from exc
        if response.is_error:
            raise _error_from_response(response)
        return response

    @staticmethod
    def _json_object(response: httpx2.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = f"malformed response from JupyterHub: {exc}"
            raise HubAPIError(None, msg) from exc
        if not isinstance(body, dict):
            msg = "malformed response from JupyterHub: not a JSON object"
            raise HubAPIError(None, msg)
        return body

    async def get_self(self, token: str) -> dict[str, Any]:
        """Return the token owner's model (``name``, expanded ``scopes``, ...)."""
        response = await self._request("GET", "/hub/api/user", token)
        return self._json_object(response)

    async def get_user(self, token: str, name: str) -> dict[str, Any]:
        """Return *name*'s user model with every named server, stopped ones included."""
        response = await self._request(
            "GET",
            f"/hub/api/users/{_quote(name)}",
            token,
            params={"include_stopped_servers": "1"},
        )
        return self._json_object(response)

    def _server_path(self, name: str, server: str) -> str:
        return f"/hub/api/users/{_quote(name)}/servers/{_quote(server)}"

    async def start_server(
        self, token: str, name: str, server: str, user_options: dict[str, Any]
    ) -> int:
        """Ask the hub to start *server*; return 201 (started) or 202 (pending)."""
        response = await self._request(
            "POST", self._server_path(name, server), token, body=user_options
        )
        return response.status_code

    async def progress_events(
        self, token: str, name: str, server: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield *server*'s spawn progress events until the hub closes the stream."""
        path = f"{self._server_path(name, server)}/progress"
        try:
            async with self._client.stream(
                "GET", path, headers=_auth(token)
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise _error_from_response(response)
                async for event in parse_progress_lines(response.aiter_lines()):
                    yield event
        except httpx2.HTTPError as exc:
            msg = f"could not reach JupyterHub: {type(exc).__name__}: {exc}"
            raise HubRequestError(msg) from exc

    async def stop_server(
        self, token: str, name: str, server: str, *, remove: bool
    ) -> int:
        """Stop *server*; with *remove*, delete its named-server entry too.

        Returns 204 (done) or 202 (stop still pending on the hub's side).
        """
        response = await self._request(
            "DELETE", self._server_path(name, server), token, body={"remove": remove}
        )
        return response.status_code

    def notebook_mcp_url(self, name: str, server: str) -> str:
        """Return the hub-proxied jupyter-mcp-server URL of *name*'s *server*."""
        return f"{self.hub_url}/user/{_quote(name)}/{_quote(server)}/mcp"

    def notebook_http_client(self, token: str) -> httpx2.AsyncClient:
        """Return a fresh client that authenticates notebook MCP calls with *token*.

        A separate client per call (not the shared one) because the token is
        per caller and the mcp SDK's streamable HTTP transport takes headers
        only from the client it is given. The caller must close it.
        """
        return httpx2.AsyncClient(
            headers=_auth(token), timeout=_NOTEBOOK_TIMEOUT, transport=self._transport
        )
