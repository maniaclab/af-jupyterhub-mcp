"""An in-memory JupyterHub REST API, served through ``httpx2.MockTransport``.

``FakeHub`` implements the slice of the JupyterHub 5.2 API this backend uses,
with the same status codes and JSON shapes as ``jupyterhub/apihandlers``:

- ``GET /hub/api/user`` (token owner model, including expanded ``scopes``)
- ``GET /hub/api/users/<name>`` (``servers`` holds active servers only, unless
  ``?include_stopped_servers`` is set)
- ``POST /hub/api/users/<name>/servers/<server>`` (201 started, 202 pending,
  400 for the named-server limit or an already-running server)
- ``GET /hub/api/users/<name>/servers/<server>/progress`` (``data: {json}``
  event stream with blank keepalive lines)
- ``DELETE /hub/api/users/<name>/servers/<server>`` (``{"remove": true}``
  deletes the entry; otherwise it is only stopped)
- ``/user/<name>/<server>/...`` (the hub proxy), forwarded to an optional
  ASGI app standing in for the notebook's jupyter-mcp-server.

Every request is recorded in ``FakeHub.requests`` so tests can assert on the
exact method, URL, headers, and body the client sent.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio
import httpx2

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

HUB_URL = "https://hub.example.org"

ALICE_TOKEN = "alice-hub-token"
BOB_TOKEN = "bob-hub-token"

FULL_SCOPES = [
    "access:servers!user=alice",
    "delete:servers!user=alice",
    "read:servers!user=alice",
    "read:users:name!user=alice",
    "servers!user=alice",
]

_SERVER_PATH = re.compile(r"^/hub/api/users/([^/]+)/servers/([^/]*)(/progress)?$")
_USER_PATH = re.compile(r"^/hub/api/users/([^/]+)$")
_PROXY_PATH = re.compile(r"^/user/([^/]+)/([^/]+)/")


def server_model(
    user: str,
    name: str,
    *,
    ready: bool = True,
    pending: str | None = None,
    user_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a server model shaped like JupyterHub's ``APIHandler.server_model``."""
    return {
        "name": name,
        "full_name": f"{user}/{name}",
        "last_activity": "2026-11-04T01:00:00.000000Z",
        "started": "2026-11-04T00:00:00.000000Z" if (ready or pending) else None,
        "pending": pending,
        "ready": ready,
        "stopped": not (ready or pending),
        "url": f"/user/{user}/{urllib.parse.quote(name)}/",
        "user_options": user_options or {},
        "progress_url": f"/hub/api/users/{user}/servers/{name}/progress",
        "full_url": None,
        "full_progress_url": None,
    }


def _error(status: int, message: str) -> httpx2.Response:
    return httpx2.Response(status, json={"status": status, "message": message})


def sse(*events: dict[str, Any]) -> bytes:
    """Encode *events* the way ``SpawnProgressAPIHandler.send_event`` does."""
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events)


@dataclass
class FakeHub:
    """In-memory JupyterHub; see the module docstring for the supported API."""

    tokens: dict[str, tuple[str, list[str]]] = field(
        default_factory=lambda: {
            ALICE_TOKEN: ("alice", list(FULL_SCOPES)),
            BOB_TOKEN: ("bob", [s.replace("alice", "bob") for s in FULL_SCOPES]),
        }
    )
    # user -> server name -> server model
    servers: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: {"alice": {}, "bob": {}}
    )
    named_server_limit: int = 10
    # Status for the next POST that starts a server: 201 (ready at once) or
    # 202 (pending; the progress stream finishes the spawn).
    spawn_status: int = 202
    # Events the progress stream sends for a pending spawn. The final
    # ``ready`` event is appended by the stream itself unless one of these is
    # ``failed``, mirroring SpawnProgressAPIHandler.
    progress_events: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"progress": 10, "message": "Server requested"},
            {"progress": 50, "message": "Pulling image"},
        ]
    )
    # When true, the progress stream sends keepalives forever (spawn hangs).
    progress_hangs: bool = False
    # When true, the progress stream closes after progress_events, sending
    # neither ready nor failed.
    progress_ends_early: bool = False
    # Force GET /hub/api/user to fail with this status (e.g. 500).
    whoami_status: int | None = None
    # Optional ASGI app standing in for every notebook's /mcp endpoint.
    notebook_app: Any = None
    requests: list[httpx2.Request] = field(default_factory=list)

    def transport(self) -> httpx2.MockTransport:
        """Return a MockTransport that serves this fake hub."""
        return httpx2.MockTransport(self.handle)

    def add_server(
        self,
        user: str,
        name: str,
        *,
        ready: bool = True,
        pending: str | None = None,
        user_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create (or replace) a server entry for *user* directly."""
        model = server_model(
            user, name, ready=ready, pending=pending, user_options=user_options
        )
        self.servers.setdefault(user, {})[name] = model
        return model

    def _owner(self, request: httpx2.Request) -> tuple[str, list[str]] | None:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() not in {"token", "bearer"}:
            return None
        return self.tokens.get(token.strip())

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Route one request, the way the hub and its proxy would."""
        self.requests.append(request)
        path = urllib.parse.unquote(request.url.path)
        owner = self._owner(request)

        if (match := _PROXY_PATH.match(path)) is not None:
            return await self._proxy(request, owner, match.group(1))
        if owner is None:
            return _error(403, "Missing or invalid credentials.")
        user, scopes = owner

        if path == "/hub/api/user" and request.method == "GET":
            if self.whoami_status is not None:
                return _error(self.whoami_status, "hub is unhappy")
            return httpx2.Response(
                200,
                json={
                    "kind": "user",
                    "name": user,
                    "admin": False,
                    "scopes": sorted(scopes),
                    "session_id": None,
                    "token_id": "a1",
                },
            )
        if (match := _USER_PATH.match(path)) is not None:
            return self._get_user(request, user, match.group(1))
        if (match := _SERVER_PATH.match(path)) is not None:
            target, name, progress = match.groups()
            if target != user:
                return _error(404, "Not Found")
            if progress:
                return self._progress(user, name)
            if request.method == "POST":
                return self._start(request, user, name)
            if request.method == "DELETE":
                return self._delete(request, user, name)
        return _error(404, "Not Found")

    def _get_user(
        self, request: httpx2.Request, user: str, target: str
    ) -> httpx2.Response:
        if target != user:
            return _error(404, "Not Found")
        include_stopped = request.url.params.get("include_stopped_servers", "0")
        servers = {
            name: model
            for name, model in self.servers.get(user, {}).items()
            if include_stopped.lower() not in {"0", "false"} or not model["stopped"]
        }
        return httpx2.Response(
            200, json={"kind": "user", "name": user, "servers": servers}
        )

    def _start(self, request: httpx2.Request, user: str, name: str) -> httpx2.Response:
        existing = self.servers.get(user, {})
        if name not in existing and len(existing) >= self.named_server_limit:
            return _error(
                400,
                f"User {user} already has the maximum of {self.named_server_limit}"
                " named servers.  One must be deleted before a new server can be"
                " created",
            )
        if name in existing and existing[name]["ready"]:
            return _error(400, f"{user}:{name} is already running")
        options = json.loads(request.content) if request.content else {}
        if self.spawn_status == 201:
            self.add_server(user, name, user_options=options)
        else:
            self.add_server(
                user, name, ready=False, pending="spawn", user_options=options
            )
        return httpx2.Response(self.spawn_status, text="")

    def _progress(self, user: str, name: str) -> httpx2.Response:
        model = self.servers.get(user, {}).get(name)
        if model is None:
            return _error(404, "Not Found")
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=self._progress_stream(model),
        )

    async def _progress_stream(self, model: dict[str, Any]) -> AsyncIterator[bytes]:
        yield b"\n\n"  # keepalive, as the hub sends before any event
        if model["ready"]:
            yield sse(self._ready_event(model))
            return
        for event in self.progress_events:
            yield sse(event)
            if event.get("failed"):
                model.update(ready=False, pending=None, stopped=True)
                return
        if self.progress_ends_early:
            return
        while self.progress_hangs:
            await anyio.sleep(0.01)
            yield b"\n\n"
        model.update(ready=True, pending=None, stopped=False)
        yield sse(self._ready_event(model))

    @staticmethod
    def _ready_event(model: dict[str, Any]) -> dict[str, Any]:
        return {
            "progress": 100,
            "ready": True,
            "message": f"Server ready at {model['url']}",
            "html_message": f"Server ready at <a href='{model['url']}'>...</a>",
            "url": model["url"],
        }

    def _delete(self, request: httpx2.Request, user: str, name: str) -> httpx2.Response:
        existing = self.servers.get(user, {})
        if name not in existing:
            return _error(404, f"{user} has no server named '{name}'")
        body = json.loads(request.content) if request.content else {}
        if body.get("remove"):
            del existing[name]
        else:
            existing[name].update(ready=False, pending=None, stopped=True)
        return httpx2.Response(204)

    async def _proxy(
        self,
        request: httpx2.Request,
        owner: tuple[str, list[str]] | None,
        target: str,
    ) -> httpx2.Response:
        if owner is None or owner[0] != target:
            return _error(403, "Forbidden")
        if self.notebook_app is None:
            return _error(503, "Service Unavailable")
        # The stand-in app serves one MCP endpoint at /mcp for every server,
        # so the /user/<name>/<server> prefix is dropped before forwarding.
        forwarded = httpx2.Request(
            request.method,
            request.url.copy_with(path="/mcp"),
            headers=request.headers,
            content=request.content,
        )
        transport = httpx2.ASGITransport(app=self.notebook_app)
        return await transport.handle_async_request(forwarded)
