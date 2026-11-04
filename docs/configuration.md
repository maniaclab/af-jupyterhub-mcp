---
icon: lucide/settings
---

# Configuration

Every setting is an environment variable read once at startup
(`af_jupyterhub_mcp.config.Settings.from_env`). The Helm chart sets each one
from a value.

| Environment variable                    | Helm value                      | Default                                                  | Meaning                                                                                                |
| --------------------------------------- | ------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `JUPYTERHUB_MCP_HUB_URL`                | `hub.url`                       | `https://jupyterhub.af.uchicago.edu`                     | Hub base URL for every API call and the `/user/<name>/<server>/mcp` notebook proxy                     |
| `JUPYTERHUB_MCP_PUBLIC_HUB_URL`         | `hub.publicUrl`                 | `hub_url`                                                | Hub base URL for links shown to users (a server's JupyterLab URL)                                      |
| `JUPYTERHUB_MCP_CPU_IMAGES`             | `notebook.images.cpu`           | `hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest` | Comma-separated images allowed with `gpus=0`                                                           |
| `JUPYTERHUB_MCP_GPU_IMAGES`             | `notebook.images.gpu`           | `hub.opensciencegrid.org/usatlas/ml-platform-gpu:latest` | Comma-separated images allowed with `gpus>0`                                                           |
| `JUPYTERHUB_MCP_IMAGE_TOOL_OVERRIDES`   | `notebook.images.toolOverrides` | none                                                     | JSON `{"<image>": ["<upstream tool id>", ...]}`; see [Tools](tools.md#images-with-different-tool-sets) |
| `JUPYTERHUB_MCP_SITES`                  | `notebook.sites`                | `local`                                                  | Comma-separated site names `create_jupyter_server` accepts                                             |
| `JUPYTERHUB_MCP_LAUNCH_TIMEOUT_SECONDS` | `notebook.launchTimeoutSeconds` | `900`                                                    | How long `create_jupyter_server` follows the spawn progress                                            |
| `JUPYTERHUB_MCP_AUTH_CACHE_TTL_SECONDS` | `auth.cacheTtlSeconds`          | `60`                                                     | How long a verified token's identity is cached                                                         |
| `JUPYTERHUB_MCP_RESOURCE_URL`           | `server.resourceUrl`            | `http://HOST:PORT`                                       | Externally visible URL of this server (`--resource-url`)                                               |

## Two hub URLs

In the cluster the backend reaches the hub through its proxy Service,
`http://proxy-public.binderhub.svc.cluster.local`. Users cannot open that
address. Set `JUPYTERHUB_MCP_HUB_URL` to the Service and
`JUPYTERHUB_MCP_PUBLIC_HUB_URL` to `https://jupyterhub.af.uchicago.edu`. API and
notebook calls then go to the Service, and links in tool results point at the
public hub. Outside the cluster, leave the public URL unset.

## Images

Every allowlisted image must include `jupyterhub-singleuser`, or the hub cannot
start it; ml_platform images have it since
[maniaclab/ml_platform#21](https://github.com/maniaclab/ml_platform/pull/21).
The `nb_*` tools also need `jupyter-mcp-server`, and the `nb_ui_*` tools need
`jupyter-mcp-tools` with each command allowlisted.

## Sites

The hub spawner (`binderspawner_mixin.py`) matches `resource_requests.site`
against the BinderHub `sites` list (`UC AF`, `ssl`, `nrp`) and falls back to the
local site for any name it does not recognise. The default `local` therefore
always works and lands on UC AF.

## Fixed guardrails

These are always enforced and are not configurable: 1 to 16 CPU cores, 1 to 256
GB of memory, `gpus >= 0`, and `gpu_product` only with `gpus > 0`. The per-user
limit of 10 named servers is the hub's own setting.

## CLI

```
af-jupyterhub-mcp serve [--host 0.0.0.0] [--port 8000] [--resource-url URL]
                        [--forwarded-allow-ips 127.0.0.1] [--log-level info]
```

HTTP transport only, with the MCP endpoint at `/mcp` and an unauthenticated
health check at `/healthz`. The server is stateless, so it can run with several
replicas.
