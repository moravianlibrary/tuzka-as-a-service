{{/*
Chart name truncated to 63 chars.
*/}}
{{- define "taas.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name.
*/}}
{{- define "taas.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "taas.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ include "taas.name" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "taas.selectorLabels" -}}
app.kubernetes.io/name: {{ include "taas.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Build the list of exposure targets (api, admin-dashboard, and legacy when compat
is enabled). Each entry: name, cfg, service, port, strip.
Usage: {{- $targets := include "taas.exposeTargets" . | fromYamlArray }}
*/}}
{{- define "taas.exposeTargets" -}}
{{- $full := include "taas.fullname" . -}}
{{- $targets := list
  (dict "name" "api" "cfg" .Values.expose.api "service" (printf "%s-api" $full) "port" (.Values.api.port | int) "strip" false)
  (dict "name" "admin-dashboard" "cfg" .Values.expose.adminDashboard "service" (printf "%s-api" $full) "port" (.Values.api.port | int) "strip" false)
-}}
{{- if .Values.compat.enabled -}}
{{- $targets = append $targets (dict "name" "legacy" "cfg" .Values.expose.legacy "service" (printf "%s-compat" $full) "port" (.Values.compat.port | int) "strip" true) -}}
{{- end -}}
{{- toYaml $targets -}}
{{- end }}

{{/*
Resolve image reference for a component.
Usage: {{ include "taas.image" (dict "image" .Values.api.image "global" .Values.image) }}
*/}}
{{- define "taas.image" -}}
{{- $registry := .global.registry -}}
{{- $repo := .image.repository -}}
{{- $tag := .image.tag | default .global.tag -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end }}
