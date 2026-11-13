"""Exceptions raised by the af-jupyterhub-mcp hub layer."""

from __future__ import annotations


class HubError(RuntimeError):
    """Base class for failures talking to JupyterHub."""


class HubRequestError(HubError):
    """The hub could not be reached (connection, TLS, or timeout failure)."""


class HubUnavailableError(HubError):
    """The hub could not answer a token check.

    Raised by ``HubTokenVerifier`` when the hub is unreachable, answers with a
    5xx, or sends a malformed reply -- anything other than a definite refusal.
    The transport answers it with 503, never 401: a 401 tells af-mcp-platform
    the token is dead, and the broker would then mark a still-valid,
    non-refreshable hub token as rejected.
    """


class HubAPIError(HubError):
    """The hub answered with an error status, or with a malformed body.

    ``status`` is the HTTP status (``None`` for a malformed 2xx body) and the
    message is the hub's own ``{"message": ...}`` text when it sent one.
    """

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class GuardrailError(ValueError):
    """A create request violates a server-side validation guardrail."""


class ImageNotAllowedError(GuardrailError):
    """The requested image is not on the chart-values-driven allowlist."""


class NameConflictError(RuntimeError):
    """The requested server name is already one of the caller's servers."""


class NotFoundOrNotYoursError(LookupError):
    """The caller has no server with this name.

    Servers are always looked up in the caller's own hub user model, so
    another user's server is indistinguishable from one that does not exist.
    """


class NotebookNotReadyError(RuntimeError):
    """The caller's server exists but is pending or stopped, not ready."""


class ToolNotSupportedByImageError(LookupError):
    """A server's image is configured (per-image tool overrides) not to offer a tool."""


class SpawnFailedError(RuntimeError):
    """The hub reported that a server failed to start."""


class SpawnTimeoutError(TimeoutError):
    """A server did not become ready within the configured launch timeout."""
