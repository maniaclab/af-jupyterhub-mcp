# af-jupyterhub-mcp — Contributor Guide

MCP server that lets AF users start, inspect, stop, and delete their own
JupyterHub named servers on jupyterhub.af.uchicago.edu and work inside them,
exposed as tools for LLMs, behind the af-mcp-platform broker.

## Architecture

```
LLM <--MCP/HTTP--> af-mcp-platform aggregator <--Bearer: linked hub token--> af-jupyterhub-mcp <--REST + hub proxy--> JupyterHub
```

**Design philosophy**: like af-jupyterlab-mcp, this backend exposes fixed, typed
tools rather than a raw API pass-through; the value is the AF policy layered on
top (image allowlist, site list, resource guardrails, per-image tool support,
token hygiene). Unlike af-jupyterlab-mcp it never talks to Kubernetes or
BinderHub: every launch, lookup, stop, and notebook call goes through the
JupyterHub REST API and hub proxy with the caller's own hub token, so servers on
remote sites (`ssl`, `nrp`) work the same as local ones.

Seven lifecycle tools (`tools/jupyterhub.py`) manage named servers. Seventeen
`nb_*` tools (`tools/nb_proxy.py`) proxy to the Datalayer `jupyter-mcp-server`
running inside the server, at `{hub_url}/user/<name>/<server>/mcp`. Fifty-two
`nb_ui_*` tools (`tools/nb_ui.py`) proxy JupyterLab frontend commands served by
the `jupyter-mcp-tools` extension; these run inside the user's open JupyterLab
tab and fail without one. 76 tools in total.

## Project layout

`src/af_jupyterhub_mcp/` splits into `auth/` (hub-token verification), `hub/`
(all JupyterHub-facing logic: the REST client, server naming/validation/
summaries/spawn following, the notebook MCP proxy, and errors), and `tools/`
(the `@mcp.tool()` registrations plus shared `_helpers.py`). `tests/` mirrors
this layout (`tests/auth/`, `tests/hub/`, `tests/tools/`), plus
`test_config.py`, `test_server.py`, `test_cli.py`, `test_integration.py` (real
app + real MCP client against the fake hub) and `test_e2e_hub.py` (live hub).
Run `tree src/af_jupyterhub_mcp tests` for the file-by-file breakdown --
deliberately not duplicated here, since hand-maintained trees go stale.

## Tool registration pattern

Mirrors af-jupyterlab-mcp: a `register(mcp: MCPServer) -> None` per module
defines all `@mcp.tool()` closures; `server.py`'s `_register_all` calls all
three.

Every tool returns markdown _and_ structured content:
`CallToolResult(content=[...], structured_content=...)`, with the return
annotation spelled `Annotated[CallToolResult, ResultModel]`. This is the escape
hatch the mcp SDK's `func_metadata()` provides specifically for this case:
annotating a tool `-> ResultModel` directly gets you `outputSchema` +
`structuredContent`, but the SDK then renders the text block as JSON, destroying
the curated markdown. `Annotated[CallToolResult, ResultModel]` publishes
`outputSchema` from `ResultModel`, validates `structured_content` against it at
runtime, and returns the `CallToolResult` unchanged.

```python
# tools/mymodule.py
from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer  # noqa: TC002 (needed at runtime for eval_str signature introspection)
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from af_jupyterhub_mcp.auth.hub import get_hub_identity
from af_jupyterhub_mcp.tools._helpers import append_next_actions, format_error


class MyToolResult(BaseModel):
    """Structured result of my_tool."""

    name: str


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="My tool",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def my_tool(
        name: str,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, MyToolResult]:
        """Tool description -- shown to the LLM as the tool's purpose."""
        try:
            identity = await get_hub_identity(ctx)
            result = await do_the_thing(ctx, identity, name)
        except Exception as exc:  # noqa: BLE001
            return format_error(exc, hints=["..."])
        text = append_next_actions(str(result), ["..."])
        payload = MyToolResult(name=name)
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=payload.model_dump(mode="json"),
        )
```

Key conventions:

- The owner of every server is **always** the hub user the caller's token
  belongs to (`get_hub_identity(ctx).name`) -- no tool takes an owner/username
  argument, ever. Every hub call is made with the caller's own token, so the hub
  enforces ownership too.
- `ctx` is keyword-only (after `*`) so optional parameters can have defaults
  before it.
- `Context`/`MCPServer`/`CallToolResult`/`TextContent`/`ToolAnnotations` and
  every result model used in a return annotation must be imported as **real,
  non-`TYPE_CHECKING`** imports (with a `# noqa: TC002` on the
  `mcp.server.mcpserver` import) -- the mcp SDK's `func_metadata()` calls
  `inspect.signature(func, eval_str=True)`, which needs every name in the
  signature to resolve at runtime. Getting this wrong raises
  `InvalidSignature: Unable to evaluate type annotations` at registration.
- Annotations follow the read-only/mutating/destructive split: read-only tools
  get `read_only_hint=True, open_world_hint=True`; mutating non-destructive
  tools get `read_only_hint=False` with `destructive_hint` unset; destructive
  tools (stop/delete, which lose kernels and unsaved state, and the
  code-executing `nb_*` tools) get
  `read_only_hint=False, destructive_hint=True`. `idempotent_hint` stays unset
  everywhere.
- Errors are returned via `format_error(exc, hints=[...])`, which itself returns
  a `CallToolResult(is_error=True)` -- never raised, never a bare
  `f"Error: {exc}"` string, and never a plain error `CallToolResult` built by
  hand at a tool's own call site. Tool docstrings are the LLM-facing
  description: keep implementation notes (lint rationale etc.) in comments.
- `tools/nb_proxy.py`'s and `tools/nb_ui.py`'s tools proxy to
  jupyter-mcp-server, which returns text for every tool, so all of them share
  one minimal wrapper model, `NbProxyResult({"result": <str>})`, via the shared
  `_call_upstream` helper. `_get_ready_server` and `call_notebook_tool` raise
  typed exceptions (`NotFoundOrNotYoursError`/`NotebookNotReadyError`/
  `NotebookToolTransportError`/`NotebookToolUpstreamError`) -- never return a
  plain error string a caller has to `isinstance`-sniff.
- Per-image tool support (`Settings.image_tool_overrides`, chart value
  `notebook.images.toolOverrides`) is enforced in `_call_upstream` against the
  server's `user_options.image`, keyed by **upstream** tool id. Images without
  an override (including BinderHub-built ones, and servers with no image in
  `user_options`) are assumed to support every tool; keep that default meaning
  "the latest ml-platform image".
- All hub I/O goes through `hub.client.HubClient` (one shared `httpx2` client
  from `server.make_app`, closed by the lifespan). Never construct an HTTP
  client in `tools/`; notebook calls get a per-call client from
  `HubClient.notebook_http_client(token)` because the mcp SDK's streamable HTTP
  transport takes headers only from its client.
- **Never return or log a token.** That covers the caller's hub token and the
  `token` BinderHub stores in `user_options`. `hub.servers.summarize_server` is
  the only way server models reach tool output, and it copies named fields only.
  Links in tool output use `Settings.public_url`, never `hub_url` (which may be
  an in-cluster address).

## Auth: the user's linked JupyterHub token

af-mcp-platform links each user's hub account (oauth21-direct, confidential
client `service-af-mcp-platform`) and forwards the token as the bearer
(`auth_type: bearer`). `auth/hub.py`'s module docstring describes the
two-lookups-per-request shape: the SDK's `token_verifier=` gates the transport,
and `get_hub_identity` re-asks the same cached `HubTokenVerifier` for the
username and checks the required scopes (`servers!user`, `access:servers!user`,
`read:users:name!user`, matched against the expanded `!user=<name>` form
`/hub/api/user` returns). The backend holds no credentials of its own. Keep the
token-verification surface in `auth/hub.py` and `server.py`.

## Launching

`create_jupyter_server` POSTs the spawner's `user_options`
(`hub.servers.build_user_options`: string values, `qos` as
`Guaranteed`/`Burstable`, `gpuModel` omitted for "any GPU" because the AF
spawner turns any value into a node selector) and, on 202, follows the progress
stream (`hub.servers.follow_spawn`), forwarding each event via
`ctx.report_progress` until ready, failed, or `launch_timeout_seconds`. A
timeout leaves the server pending; never delete it implicitly.

Named-server rules: new names are DNS labels (`validate_new_server_name`);
lookups accept anything the hub can address (`check_server_name`), because the
BinderHub UI creates names with underscores. The hub caps users at 10 named
servers, stopped ones included -- which is why stop (keep the entry) and delete
(`{"remove": true}`) are separate tools.

## Build and test commands

```bash
pixi run test          # unit + integration tests (fake hub, no network)
pixi run -e py312 test-e2e  # live hub; needs AF_JUPYTERHUB_E2E_TOKEN
pixi run lint          # pre-commit + pylint
pixi run helm-lint     # lint + smoke-render the Helm chart
pixi run build         # build sdist + wheel
```

## Adding a new tool

1. Add a new `@mcp.tool(annotations=ToolAnnotations(...))` function inside the
   matching module's `register()` (or a new module + `server.py`'s
   `_register_all` entry).
2. Define a pydantic `BaseModel` for the tool's structured result and spell the
   return annotation `Annotated[CallToolResult, ResultModel]`; assign
   `ToolAnnotations` per the read-only/mutating/destructive split.
3. Any new hub call belongs in `hub/client.py` (and any policy in
   `hub/servers.py`) -- never call `httpx2` directly from `tools/`.
4. Write unit tests against `tests/fakes.py`'s `FakeHub` (extend it to mirror
   the JupyterHub 5.2 handler you call, status codes and all), using the
   `make_ctx` and `tool_text` fixtures from `tests/conftest.py`. No network
   access is expected in unit or integration tests.
5. Update the tool-count assertions (`tests/test_server.py`'s `TOOL_COUNT`, the
   per-module bucket tests) and the README/docs tool tables.
6. Run `pixi run test` and `pixi run lint` to verify.

### Adding a JupyterLab UI (`nb_ui_*`) tool

1. Confirm the command id exists in the JupyterLab version the notebook image
   ships (grep the JupyterLab source for `'namespace:command'`) — the upstream
   jupyter-mcp-tools README lists several ids that do not. Skip commands that
   act on the human's browser or block on a modal dialog (see `nb_ui.py`'s
   module docstring).
2. Allowlist the upstream id (`namespace_command`) in the notebook image's
   `allowed_jupyter_mcp_tools`
   ([maniaclab/ml_platform](https://github.com/maniaclab/ml_platform)'s
   `config/jupyter_notebook_config.py`). Older images that do not offer it need
   no change -- their `notebook.images.toolOverrides` entry already lists only
   what they support -- but an override for an image that does offer it must
   gain the id.
3. If the command takes no arguments, add a `UiToolSpec` row to `_UI_TOOLS` —
   the proxy name is derived from the id. Otherwise add a typed wrapper in
   `nb_ui.register()` with `name=_ui_name(...)`,
   `description=_description(...)`, and `annotations=_annotations(...)`.
4. Add the id to the matching bucket (and to `_TYPED` if it takes arguments) in
   `tests/tools/test_nb_ui.py`, update the tool counts there and in
   `tests/test_server.py`, and add the row to `docs/tools.md`'s `nb_ui_*` table.
