---
icon: lucide/code
---

# Contributing

## Architecture

```
LLM <--MCP/HTTP--> af-mcp-platform aggregator <--Bearer: linked hub token--> af-jupyterhub-mcp <--REST + proxy--> JupyterHub
```

`src/af_jupyterhub_mcp/` has four parts:

- `auth/hub.py`: `HubTokenVerifier`, scope checks, and `get_hub_identity`.
- `hub/`: everything that talks to JupyterHub.
  - `client.py` is the REST client and progress-stream parser.
  - `servers.py` holds naming, launch validation, `user_options`, summaries, and
    `follow_spawn`.
  - `proxy.py` is the notebook MCP client.
  - `errors.py` holds the exceptions.
- `tools/`: the `@mcp.tool()` registrations, in `jupyterhub.py`, `nb_proxy.py`
  and `nb_ui.py`.
- `server.py` and `cli.py`: the app and the `serve` command.

`tests/` mirrors this layout. `tests/fakes.py` is an in-memory JupyterHub
(`httpx2.MockTransport`) whose proxy serves a real mcp-SDK server, so unit and
integration tests need no network. See `CLAUDE.md` for the conventions every
tool follows.

## Development setup

```bash
git clone https://github.com/maniaclab/af-jupyterhub-mcp
cd af-jupyterhub-mcp
pixi install
pixi run pre-commit-install
```

## Build and test commands

```bash
pixi run test                 # unit + integration (fake hub, no network)
pixi run -e py310 test        # any of py310..py314
AF_JUPYTERHUB_E2E_TOKEN=... pixi run -e py312 test-e2e   # live hub, no mocks
pixi run lint                 # pre-commit (prek) + pylint
pixi run helm-lint            # lint + smoke-render the Helm chart
pixi run check-docstrings     # pydocstyle
pixi run docs-serve           # build and serve docs locally
pixi run build                # build sdist + wheel
```

The e2e test starts a named server `mcp-e2e` for the token's owner, runs
`nb_list_files` and `nb_execute_code("print(1+1)")` in it, then deletes it.
`AF_JUPYTERHUB_E2E_IMAGE` picks the image, defaulting to
`hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest`. Mint the token at
`/hub/token` with the three required scopes.

## Releases

Giordon tags releases with `pixi run -e dev tbump <version>`. That bumps
`tbump.toml`, the chart's `version` and `appVersion`, and
`jupyterhubMcp.version` together. Publishing a GitHub release uploads the
package to PyPI through trusted publishing.
