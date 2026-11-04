# af-jupyterhub-mcp

<!-- --8<-- [start:intro] -->

MCP server that lets UChicago ATLAS Analysis Facility users start, inspect,
stop, and delete their own JupyterHub named servers on
[jupyterhub.af.uchicago.edu](https://jupyterhub.af.uchicago.edu), and work
inside them (run code, edit notebooks, drive the JupyterLab UI), as tools for
LLMs behind the [af-mcp-platform](https://github.com/maniaclab/af-mcp-platform)
broker.

<!-- --8<-- [end:intro] -->

<!-- --8<-- [start:architecture] -->

## Architecture

```
LLM <--MCP/HTTP--> af-mcp-platform aggregator --Bearer: linked hub token--> af-jupyterhub-mcp
                                                                              |
                                      JupyterHub REST API  <------------------+
                                      /hub/api/users/<you>/servers/<name>     |
                                      hub proxy /user/<you>/<name>/mcp  <-----+--> jupyter-mcp-server
```

Everything goes through JupyterHub and nothing else. Servers are started with
`POST /hub/api/users/<you>/servers/<name>`, whose body is the spawner's
`user_options` (image, site, CPU, memory, QoS, GPUs). The tool then follows the
hub's spawn progress stream, sending MCP progress notifications until the server
is ready. Notebook tools reach the server's own `jupyter-mcp-server` through the
hub proxy, so they work for servers on any site (`local`, `ssl`, `nrp`). The
backend never talks to Kubernetes or BinderHub. Servers launched from the
BinderHub UI are ordinary named servers of the same user, so they show up and
work here too.

Launched images must include `jupyterhub-singleuser` (ml_platform has since
[#21](https://github.com/maniaclab/ml_platform/pull/21)) and
`jupyter-mcp-server` (plus `jupyter-mcp-tools` for the `nb_ui_*` tools).

<!-- --8<-- [end:architecture] -->

<!-- --8<-- [start:auth] -->

## Authentication

- **The user's own hub token.** af-mcp-platform links each user's JupyterHub
  account through the hub's OAuth (`oauth21-direct`, confidential client
  `service-af-mcp-platform`). It forwards the linked token as
  `Authorization: Bearer <hub token>`. The backend has no credentials of its own
  and uses that token for every hub and notebook call, so its trust tier is
  `user-tier`.
- **Identity.** The caller is resolved with `GET /hub/api/user`. The result is
  cached for 60 s, keyed by the token's SHA-256. The hub username is the owner
  of every server a tool touches; no tool takes an owner argument.
- **Required scopes.** `servers!user`, `access:servers!user` and
  `read:users:name!user`. A token without them gets an error that names the
  missing scopes.
- **Token lifetime.** The link token uses the hub's default 14-day lifetime. The
  hub issues no refresh token, so users re-link when it expires; the tools then
  say to re-link.
- **Secrets stay out of results.** Tokens never appear in tool results or logs.
  That includes the `token` BinderHub stores in a server's `user_options`.

<!-- --8<-- [end:auth] -->

## Tools

76 tools: seven for the server lifecycle, 17 `nb_*` notebook tools, and 52
`nb_ui_*` JupyterLab UI tools. See [docs/tools.md](docs/tools.md) for the full
reference.

| Tool                    | Does                                                             | Kind        |
| ----------------------- | ---------------------------------------------------------------- | ----------- |
| `create_jupyter_server` | Start a named server and wait until it is ready, with progress   | mutating    |
| `list_jupyter_servers`  | List your named servers, stopped ones included                   | read-only   |
| `get_jupyter_server`    | Show one server's status, image, resources, and URL              | read-only   |
| `stop_jupyter_server`   | Stop a server but keep its entry (it still counts toward the 10) | destructive |
| `delete_jupyter_server` | Stop a server and remove its entry                               | destructive |
| `list_supported_images` | List the CPU and GPU images `create_jupyter_server` accepts      | read-only   |
| `list_sites`            | List the site names `create_jupyter_server` accepts              | read-only   |

## Build and test commands

```bash
pixi run test          # unit + integration tests (fake hub, no network)
pixi run -e py312 test-e2e  # live hub; needs AF_JUPYTERHUB_E2E_TOKEN
pixi run lint          # pre-commit + pylint
pixi run helm-lint     # lint + smoke-render the Helm chart
pixi run docs-serve    # build and serve the docs
```

See [docs/configuration.md](docs/configuration.md) and
[docs/deployment.md](docs/deployment.md) for running it.
