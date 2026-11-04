{{/*
Expand the name of the chart.
*/}}
{{- define "af-jupyterhub-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "af-jupyterhub-mcp.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name and version label value.
*/}}
{{- define "af-jupyterhub-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "af-jupyterhub-mcp.labels" -}}
helm.sh/chart: {{ include "af-jupyterhub-mcp.chart" . }}
{{ include "af-jupyterhub-mcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels. Kept stable (name + instance) so external selectors keyed on
app.kubernetes.io/name=af-jupyterhub-mcp continue to match.
*/}}
{{- define "af-jupyterhub-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "af-jupyterhub-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name to use.
*/}}
{{- define "af-jupyterhub-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "af-jupyterhub-mcp.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Validate the configuration. Fails the render early with a clear message
rather than producing a manifest the server would reject at startup.
*/}}
{{- define "af-jupyterhub-mcp.validate" -}}
{{- if not .Values.hub.url -}}
  {{- fail "hub.url is required (the JupyterHub this backend drives)" -}}
{{- end -}}
{{- if not .Values.notebook.images.cpu -}}
  {{- fail "notebook.images.cpu must list at least one image" -}}
{{- end -}}
{{- if not .Values.notebook.sites -}}
  {{- fail "notebook.sites must list at least one site (e.g. local)" -}}
{{- end -}}
{{- if .Values.ingress.enabled -}}
  {{- if not .Values.ingress.host -}}
    {{- fail "ingress.enabled=true requires ingress.host" -}}
  {{- end -}}
{{- end -}}
{{- end }}

{{/*
Public resource URL: explicit value, else derived from the ingress host, else
empty (the server falls back to http://<host>:<port>).
*/}}
{{- define "af-jupyterhub-mcp.resourceUrl" -}}
{{- if .Values.server.resourceUrl -}}
{{- .Values.server.resourceUrl -}}
{{- else if and .Values.ingress.enabled .Values.ingress.host -}}
{{- printf "https://%s" .Values.ingress.host -}}
{{- end -}}
{{- end }}

{{/*
Build the `af-jupyterhub-mcp serve` argv as a space-joined string for the
main container's `/bin/sh -c` script. Every value here is chart-controlled
and contains no shell metacharacters except forwardedAllowIps, which is
single-quoted so a "*" is not glob-expanded.
*/}}
{{- define "af-jupyterhub-mcp.serveArgs" -}}
{{- include "af-jupyterhub-mcp.validate" . -}}
{{- $args := list "serve"
    "--host" (.Values.server.host | toString)
    "--port" (.Values.server.port | toString)
    "--forwarded-allow-ips" (printf "'%s'" .Values.forwardedAllowIps)
    "--log-level" .Values.logLevel -}}
{{- with (include "af-jupyterhub-mcp.resourceUrl" .) -}}
{{- $args = append $args "--resource-url" -}}
{{- $args = append $args . -}}
{{- end -}}
{{- $args | join " " -}}
{{- end }}
