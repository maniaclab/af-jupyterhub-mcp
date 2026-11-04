"""Caller authentication with the caller's own JupyterHub token.

af-mcp-platform's broker links each user's JupyterHub account (an OAuth
client of the hub) and forwards the linked hub token to this backend as
``Authorization: Bearer <hub token>`` (``auth_type: bearer``). There is no
second credential: the backend holds none of its own and uses that same
token for every hub API and notebook call it makes on the caller's behalf.

Two places read the bearer, both through one ``HubTokenVerifier``:

1. The mcp SDK's ``token_verifier=`` gates transport-level access. An
   unknown or expired token gets a 401 before any tool runs.
2. Each tool calls ``get_hub_identity(ctx)``, which re-reads the bearer and
   asks the same verifier again. That is a cache hit (keyed by the token's
   SHA-256, short TTL), so it costs no extra hub round trip. It returns the
   hub username, which is the owner of every server a tool touches. No
   tool takes an owner argument. It also checks the token's scopes and
   names any missing ones, a clearer message than a bare transport 401.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp.server.auth.provider import AccessToken

from af_jupyterhub_mcp.hub.errors import HubError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from af_jupyterhub_mcp.hub.client import HubClient

log = logging.getLogger(__name__)

# Scopes the linked hub token must carry: start/stop/remove the caller's
# servers, reach them through the hub proxy, and read the caller's own name.
# These match the af-mcp-platform OAuth client's allowed scopes.
REQUIRED_SCOPES: tuple[str, ...] = (
    "servers!user",
    "access:servers!user",
    "read:users:name!user",
)

_RELINK_HINT = "re-link your JupyterHub account in af-mcp-platform"


class MissingScopesError(PermissionError):
    """The hub token is valid but lacks scopes this backend needs."""


def missing_scopes(scopes: Sequence[str], name: str) -> list[str]:
    """Return the entries of ``REQUIRED_SCOPES`` that *scopes* do not grant to *name*.

    ``/hub/api/user`` returns *expanded* scopes, so the ``!user`` filter of
    the requested scope shows up as ``!user=<name>``. A required
    ``base!user`` is satisfied by ``base!user=<name>``, the literal
    ``base!user``, or the unfiltered ``base``. Narrower filters (for example
    ``!server=<name>/<server>``) and other users' filters do not satisfy it.
    """
    granted = set(scopes)
    missing = []
    for required in REQUIRED_SCOPES:
        base, _, _filter = required.partition("!")
        accepted = {required, base, f"{required}={name}"}
        if not accepted & granted:
            missing.append(required)
    return missing


@dataclass(frozen=True)
class HubIdentity:
    """The verified caller: hub username plus the token to act with."""

    name: str
    token: str = field(repr=False)


@dataclass
class _CacheEntry:
    access: AccessToken
    expires: float


class HubTokenVerifier:
    """mcp ``TokenVerifier`` that resolves a hub token via ``GET /hub/api/user``.

    Successful lookups are cached for *ttl_seconds*, keyed by the token's
    SHA-256 so the raw token is never a dict key. Rejections are not cached,
    so a token that failed during a hub outage works again once the hub
    recovers.
    """

    def __init__(
        self,
        hub: HubClient,
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hub = hub
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}

    def _prune(self, now: float) -> None:
        for key in [k for k, e in self._cache.items() if e.expires <= now]:
            del self._cache[key]

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the hub identity for *token*, or None if the hub rejects it."""
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        self._prune(now)
        if (entry := self._cache.get(key)) is not None:
            return entry.access
        try:
            model = await self._hub.get_self(token)
        except HubError as exc:
            log.warning("JupyterHub rejected or failed a token lookup: %s", exc)
            return None
        name = model.get("name")
        if not isinstance(name, str) or not name or model.get("kind", "user") != "user":
            log.warning("JupyterHub token does not belong to a user; rejecting")
            return None
        scopes = [s for s in model.get("scopes", []) if isinstance(s, str)]
        access = AccessToken(
            token=token, client_id="jupyterhub", scopes=scopes, subject=name
        )
        self._cache[key] = _CacheEntry(access=access, expires=now + self._ttl)
        return access


def _extract_bearer(ctx: Any) -> str:
    """Return the bearer token from the current request's Authorization header."""
    auth = ctx.request_context.request.headers.get("authorization", "") or ""
    if not auth.lower().startswith("bearer "):
        msg = "Missing Bearer token in Authorization header"
        raise PermissionError(msg)
    return str(auth[7:].strip())


async def get_hub_identity(ctx: Any) -> HubIdentity:
    """Return the verified caller for the current request.

    Raises:
        PermissionError: no Bearer header, or the hub no longer accepts the
            token (expired or revoked link).
        MissingScopesError: the token is valid but lacks some of
            ``REQUIRED_SCOPES``; the message names them.
    """
    token = _extract_bearer(ctx)
    verifier: HubTokenVerifier = ctx.request_context.lifespan_context["verifier"]
    access = await verifier.verify_token(token)
    if access is None or access.subject is None:
        msg = f"JupyterHub no longer accepts this token; {_RELINK_HINT}"
        raise PermissionError(msg)
    missing = missing_scopes(access.scopes, access.subject)
    if missing:
        msg = (
            f"the linked JupyterHub token is missing required scopes: "
            f"{', '.join(missing)}; {_RELINK_HINT} to grant them"
        )
        raise MissingScopesError(msg)
    return HubIdentity(name=access.subject, token=token)
