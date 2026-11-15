---
icon: lucide/server
---

# Deployment

A deployment has three parts:

1. a JupyterHub OAuth client for af-mcp-platform;
2. an af-mcp-platform identity provider and aggregator service that forward the
   linked hub token;
3. this chart.

## 1. JupyterHub service (OAuth client)

af-mcp-platform links each user's hub account as an OAuth client of the hub.
Register it under `hub.services` in the JupyterHub Helm values:

```yaml
jupyterhub:
  hub:
    services:
      af-mcp-platform:
        oauth_client_id: service-af-mcp-platform
        oauth_redirect_uri: https://mcp.af.uchicago.edu/v1/oauth/callback/jupyterhub
        oauth_no_confirm: true
        oauth_client_allowed_scopes:
          - servers!user
          - access:servers!user
          - read:users:name!user
        # apiToken: set from a secret (see below)
```

- **The client secret.** JupyterHub uses the service's `api_token` as the OAuth
  client secret. Generate one random value, for example with
  `openssl rand -hex 32`, and store it in two places:
  - the hub's values, as the service `apiToken`;
  - the af-mcp-platform namespace, as the `client-secret` key of the Secret that
    the identity provider's `existingClientSecretSecret` names.

  Seal or encrypt both with whatever the cluster uses for secrets in git (for
  example, `kubectl create secret generic ... --dry-run=client -o yaml` piped
  into `kubeseal`, once for each namespace), and never commit the raw value.

- **Allowed scopes.** These are exactly the scopes this backend requires: start,
  stop and remove the user's own servers; reach them through the hub proxy; and
  read the user's own name. A linked token with fewer scopes gets a tool error
  that names the missing ones.
- **Token lifetime.** Tokens from this client last the hub's default 14 days.
  There is no refresh token, so users re-link from the af-mcp-platform portal
  when theirs expires. The backend answers HTTP 401 only when the hub refuses a
  token. If the hub cannot be reached, it answers 503, so a hub outage never
  gets a user's link marked rejected.

## 2. af-mcp-platform

- **Identity provider.** Add an `oauth21-direct` identity provider with:
  - alias `jupyterhub`;
  - authorize endpoint
    `https://jupyterhub.af.uchicago.edu/hub/api/oauth2/authorize`;
  - token endpoint `https://jupyterhub.af.uchicago.edu/hub/api/oauth2/token`;
  - `clientId: service-af-mcp-platform`;
  - `existingClientSecretSecret` naming the Secret above;
  - an explicit `scope` listing the three scopes;
  - this backend's aggregator service as its target.
- **Aggregator service.** Add an entry pointing at this chart's Service
  (`http://<release>.<namespace>.svc.cluster.local:80/mcp`) with:
  - `auth_type: bearer`, so the broker forwards the linked hub token as the
    bearer;
  - `trust_tier: user-tier`;
  - per-tool `required_permission` and `agent_policy` entries. The tool names
    are listed in [Tools](tools.md).
- **Timeouts.** `create_jupyter_server` holds its request open until the server
  is ready. Give the aggregator a per-call timeout above
  `notebook.launchTimeoutSeconds`, which defaults to 900 s.

## 3. Helm chart

```bash
helm install jupyterhub-mcp charts/af-jupyterhub-mcp \
  --namespace mcp \
  --set hub.url=http://proxy-public.binderhub.svc.cluster.local \
  --set hub.publicUrl=https://jupyterhub.af.uchicago.edu
```

The chart installs the pinned conda-forge release (`jupyterhubMcp.version`) into
the generic pixi image at pod start. It has no RBAC and mounts no ServiceAccount
token. See the chart README (`charts/af-jupyterhub-mcp/README.md` in the
repository) for every value, and [Configuration](configuration.md) for the
environment variables they set.

If the `mcp` namespace restricts egress, allow traffic to the hub's proxy
Service (`proxy-public` in the `binderhub` namespace). That is the only
destination this backend talks to.
