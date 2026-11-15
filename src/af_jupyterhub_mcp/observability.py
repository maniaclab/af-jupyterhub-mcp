"""Request id, JSON logs and access log (af-mcp-platform docs/mcp-design.md L1-L3).

The broker sends its correlation id as ``X-Request-ID`` on every request.
``RequestLogMiddleware`` binds it once at the HTTP edge into a context
variable; ``JsonFormatter`` stamps it on every log line written while that
request is served, so tool and library code carry it without passing it
around. ``ToolCallMiddleware`` writes the one ``tool.call`` line per tool
invocation.

Never log a token, a credential, or a tool's arguments or results.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TextIO

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.context import ServerMiddleware
from starlette.datastructures import Headers

if TYPE_CHECKING:
    from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEALTH_PATH = "/healthz"
MAX_REQUEST_ID_CHARS = 128

log = logging.getLogger("af_jupyterhub_mcp")

#: The broker's X-Request-ID for the request being served.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    """One JSON object per line with the standard fields.

    Call-specific fields travel as ``extra={"fields": {...}}``.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render *record* as a single JSON line."""
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        if request_id := request_id_var.get():
            entry["request_id"] = request_id
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(log_level: str, stream: TextIO | None = None) -> None:
    """Send every log line, this server's and uvicorn's, to *stream* (stdout) as JSON."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(handlers=[handler], force=True)
    logging.getLogger().setLevel(log_level.upper())


class RequestLogMiddleware:
    """Bind ``X-Request-ID`` for the request and write its access line.

    The broker's id is authoritative; one is generated only when the header is
    absent, so every line still carries an id. A health probe that succeeds
    writes no access line (the orchestrator probes every few seconds, and a
    line per probe would bury the rest); a failing one does.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one request inside its request-id context."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = Headers(scope=scope).get("x-request-id", "")
        reset = request_id_var.set(header[:MAX_REQUEST_ID_CHARS] or uuid.uuid4().hex)
        status = 500
        started = time.perf_counter()

        async def send_and_note_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_note_status)
        finally:
            if scope["path"] != HEALTH_PATH or status >= 400:
                log.info(
                    "http.request",
                    extra={
                        "fields": {
                            "method": scope["method"],
                            "path": scope["path"],
                            "status": status,
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000, 1
                            ),
                        }
                    },
                )
            request_id_var.reset(reset)


class ToolCallMiddleware(ServerMiddleware[Any]):
    """One ``tool.call`` log line per tool invocation."""

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        """Time a ``tools/call`` and log its subject, tool and outcome."""
        if ctx.method != "tools/call":
            return await call_next(ctx)
        name = (ctx.params or {}).get("name")
        token = get_access_token()
        # The hub username HubTokenVerifier resolved for the bearer.
        subject = token.subject if token else None
        outcome = "error"
        started = time.perf_counter()
        try:
            result = await call_next(ctx)
            # Tools report their failures as an isError result, not an
            # exception, and the middleware sees the result as its wire dict.
            if not (isinstance(result, dict) and result.get("isError")):
                outcome = "ok"
        finally:
            log.info(
                "tool.call",
                extra={
                    "fields": {
                        "subject": subject,
                        "tool": name,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "outcome": outcome,
                    }
                },
            )
        return result
