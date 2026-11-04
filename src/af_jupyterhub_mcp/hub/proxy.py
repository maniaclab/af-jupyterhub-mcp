"""Async MCP proxy client for calling tools on a server's jupyter-mcp-server.

Each notebook server ships ``jupyter-mcp-server`` at
``{hub_url}/user/<name>/<server>/mcp``, behind the JupyterHub proxy, which
authenticates the caller's hub token (``access:servers!user``) and works for
servers on any site. ``call_notebook_tool`` takes an ``httpx2.AsyncClient``
that already carries ``Authorization: token <hub token>``
(``HubClient.notebook_http_client``): the token travels as a header, never in
the URL, and is never returned to the caller. The MCP SDK's
``streamable_http_client`` handles the protocol handshake (initialize +
tools/call); jupyter-mcp-server runs with ``stateless_http=True``, so each
request is independent.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

if TYPE_CHECKING:
    import httpx2

if sys.version_info >= (3, 11):
    _BaseExceptionGroup = BaseExceptionGroup  # noqa: F821 -- builtin since 3.11
else:
    from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup


def _describe_exception(exc: BaseException) -> str:
    """Return a one-line ``Type: message`` description of *exc*.

    anyio's ``TaskGroup`` (used internally by ``streamable_http_client`` and
    ``ClientSession``) wraps every failure -- connection errors, HTTP status
    errors, protocol errors -- in a ``BaseExceptionGroup``, whose own
    ``str()`` is the uninformative "unhandled errors in a TaskGroup
    (N sub-exceptions)". Recursing into ``.exceptions`` surfaces the real
    leaf failure(s) instead.
    """
    if isinstance(exc, _BaseExceptionGroup):
        return "; ".join(_describe_exception(sub) for sub in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


class NotebookToolTransportError(RuntimeError):
    """The upstream jupyter-mcp-server call failed at the transport/protocol level.

    Covers connection failures, handshake failures, hub proxy refusals, and
    any other exception raised by the MCP client itself (not a tool-level
    error reported by jupyter-mcp-server -- see ``NotebookToolUpstreamError``
    for that).
    """


class NotebookToolUpstreamError(RuntimeError):
    """jupyter-mcp-server itself reported a tool-level error (``CallToolResult.is_error``)."""


async def call_notebook_tool(
    *,
    http_client: httpx2.AsyncClient,
    mcp_url: str,
    tool_name: str,
    tool_args: dict[str, object],
) -> str:
    """Call a tool on the server's jupyter-mcp-server and return its text output.

    `tool_name` is the upstream tool name exactly as jupyter-mcp-server
    registers it (e.g. `"execute_code"`).

    Returns the text content of the tool result on success. Raises
    ``NotebookToolTransportError`` on a transport/protocol failure, or
    ``NotebookToolUpstreamError`` if jupyter-mcp-server reports its own
    tool-level error -- callers are expected to catch these and format them
    via ``format_error`` (never a bare string sniffed by ``isinstance``).
    """
    try:
        async with (  # pylint: disable=used-before-assignment
            streamable_http_client(mcp_url, http_client=http_client) as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool(tool_name, tool_args)
    except Exception as exc:
        raise NotebookToolTransportError(_describe_exception(exc)) from exc

    # Extract text from the result content blocks.
    texts = [c.text for c in result.content if hasattr(c, "text")]
    output = "\n".join(texts) if texts else "(no output)"

    if result.is_error:
        raise NotebookToolUpstreamError(output)

    return output
