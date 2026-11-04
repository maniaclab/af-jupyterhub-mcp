# af-jupyterhub-mcp Helm chart

Deploys af-jupyterhub-mcp: per-user JupyterHub named servers on the ATLAS
Analysis Facility, behind af-mcp-platform, which forwards each user's linked
JupyterHub token.

## Install

```bash
helm install af-jupyterhub-mcp charts/af-jupyterhub-mcp \
  --namespace mcp \
  --set hub.url=http://proxy-public.binderhub.svc.cluster.local \
  --set hub.publicUrl=https://jupyterhub.af.uchicago.edu
```

Nothing is strictly required: `hub.url` defaults to the public AF hub. The chart
fails the render with a clear message if `hub.url`, `notebook.images.cpu` or
`notebook.sites` is emptied, or if `ingress.enabled=true` has no `ingress.host`.

## Hub URLs

- `hub.url` (`JUPYTERHUB_MCP_HUB_URL`): where the backend sends every hub API
  call and every `/user/<name>/<server>/mcp` notebook call. In the cluster, use
  the hub's proxy Service, `http://proxy-public.binderhub.svc.cluster.local`.
- `hub.publicUrl` (`JUPYTERHUB_MCP_PUBLIC_HUB_URL`): used only for links shown
  to users, such as a server's JupyterLab URL. Empty means `hub.url`. Set it to
  `https://jupyterhub.af.uchicago.edu` whenever `hub.url` is an in-cluster
  address.

## Platform wiring (af-mcp-platform)

This chart deploys the backend only. af-mcp-platform must:

1. Link the user's JupyterHub account through an `oauth21-direct` identity
   provider using the confidential client `service-af-mcp-platform`. That is a
   JupyterHub `hub.services` entry; see the deployment docs.
2. Route an aggregator service to this chart's Service with `auth_type: bearer`.
   The broker then sends the linked hub token as `Authorization: Bearer ...`,
   and that token is the only credential this backend uses.

The service is `user-tier`: it acts only with the calling user's own hub token.

## Values of note

- `notebook.images.cpu` / `notebook.images.gpu`: the image allowlist
  `create_jupyter_server` enforces. `gpus=0` needs a CPU image and `gpus>0` a
  GPU image. Images must include `jupyterhub-singleuser` (ml_platform has since
  #21) and `jupyter-mcp-server` for the `nb_*` tools.
- `notebook.images.toolOverrides`: per-image tool support for the `nb_*` /
  `nb_ui_*` tools. Keys are the exact image string in a server's
  `user_options.image`; values list the upstream tool ids that image offers.
  Images without an entry, including BinderHub-built ones, get every tool. An
  empty list means none. `values.yaml` has a commented example for the 2026.8.17
  images.
- `notebook.sites`: site names `create_jupyter_server` accepts and `list_sites`
  shows. The default is `[local]`. The hub spawner falls back to its local site
  for names it does not know.
- `notebook.launchTimeoutSeconds`: how long `create_jupyter_server` follows the
  spawn progress. The default is 900.
- `auth.cacheTtlSeconds`: how long a verified token's identity is cached. The
  default is 60.
- `ingress.enabled`: `false` by default. The aggregator calls the `Service`
  in-cluster.

## No RBAC

The server never talks to the Kubernetes API. The chart creates no Role or
ClusterRole, and the ServiceAccount has `automountServiceAccountToken: false`.

## Chart shape

Like af-jupyterlab-mcp, af-jupyterhub-mcp ships no OCI image. The chart runs the
generic `ghcr.io/prefix-dev/pixi` image, and an init container installs the
`af-jupyterhub-mcp` conda-forge release at pod startup (see the `pixi-install`
init container in `templates/deployment.yaml`). The version is pinned by
`jupyterhubMcp.version`. `tbump` keeps `Chart.yaml`'s `version` and `appVersion`
in step with that value.
