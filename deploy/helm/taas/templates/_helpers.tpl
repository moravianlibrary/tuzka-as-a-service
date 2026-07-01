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
Build the list of exposure targets (api, admin-dashboard, minio-results, and legacy
when compat is enabled). Each entry: name, cfg, service, port, strip.
minio-results is exposed so external download clients can reach presigned result URLs
(served at root — S3 path-style puts the bucket in the path, so it must NOT be stripped).
Usage: {{- $targets := include "taas.exposeTargets" . | fromYamlArray }}
*/}}
{{- define "taas.exposeTargets" -}}
{{- $full := include "taas.fullname" . -}}
{{- $targets := list
  (dict "name" "api" "cfg" .Values.expose.api "service" (printf "%s-api" $full) "port" (.Values.api.port | int) "strip" false)
  (dict "name" "admin-dashboard" "cfg" .Values.expose.adminDashboard "service" (printf "%s-api" $full) "port" (.Values.api.port | int) "strip" false)
  (dict "name" "minio-results" "cfg" .Values.expose.minioResults "service" (printf "%s-minio-results" $full) "port" (.Values.minio.port | int) "strip" false)
-}}
{{- if .Values.compat.enabled -}}
{{- $targets = append $targets (dict "name" "legacy" "cfg" .Values.expose.legacy "service" (printf "%s-compat" $full) "port" (.Values.compat.port | int) "strip" true) -}}
{{- end -}}
{{- toYaml $targets -}}
{{- end }}

{{/*
Resolve the externally-reachable results-MinIO URL used to presign download links.
Precedence: an explicit minio.results.publicUrl wins; otherwise, if minio-results is
exposed via ingress/gateway with a host, derive it from that host (https when tls is
configured, else http). Empty result => the app falls back to the in-cluster URL
(reachable only from inside the cluster). No trailing slash.
Usage: {{ include "taas.resultsPublicUrl" . }}
*/}}
{{- define "taas.resultsPublicUrl" -}}
{{- if .Values.minio.results.publicUrl -}}
{{- .Values.minio.results.publicUrl | trimSuffix "/" -}}
{{- else if and (ne .Values.expose.minioResults.kind "none") .Values.expose.minioResults.host -}}
{{- $scheme := ternary "https" "http" (gt (len .Values.expose.minioResults.tls) 0) -}}
{{- printf "%s://%s" $scheme .Values.expose.minioResults.host -}}
{{- end -}}
{{- end }}

{{/*
Off-cluster tunnel Service names. Referenced by multiple templates (the Service
itself, the backend-register Job's URL, the exporter ServiceMonitor), so they live
here to stay in lockstep — a rename here updates every consumer at once.
  engine:   <fullname>-tunnel-engine-<box>-<engine>
  exporter: <fullname>-tunnel-box-<box>-<exporter>
Usage: include "taas.tunnelEngineService" (dict "root" $ "box" $bn "engine" $en)
*/}}
{{- define "taas.tunnelEngineService" -}}
{{- printf "%s-tunnel-engine-%s-%s" (include "taas.fullname" .root) .box .engine -}}
{{- end }}
{{- define "taas.tunnelExporterService" -}}
{{- printf "%s-tunnel-box-%s-%s" (include "taas.fullname" .root) .box .exporter -}}
{{- end }}

{{/*
Validate .Values.tunnelBoxes: every box/engine/exporter name a DNS-1123 label, every
remotePort unique across ALL boxes (frps opens one listener per port, so dupes collide),
required fields present, and each rendered Service name within the 63-char limit. Emits
nothing; fails the render on the first problem. Included by every template that consumes
tunnelBoxes, so the checks can't be bypassed by rendering one template in isolation.
Usage: {{- include "taas.validateTunnelBoxes" . -}}
*/}}
{{- define "taas.validateTunnelBoxes" -}}
{{- $root := . -}}
{{- $boxNames := list -}}
{{- $ports := list -}}
{{- $labelRe := "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" -}}
{{- range $box := .Values.tunnelBoxes -}}
{{- $bn := required "each tunnelBoxes entry needs a name" $box.name -}}
{{- if not (regexMatch $labelRe $bn) }}{{- fail (printf "tunnelBoxes name %q is not a valid DNS-1123 label" $bn) }}{{- end -}}
{{- if has $bn $boxNames }}{{- fail (printf "tunnelBoxes name %q is used more than once — each box needs a unique name" $bn) }}{{- end -}}
{{- $boxNames = append $boxNames $bn -}}
{{- $engNames := list -}}
{{- range $e := ($box.engines | default list) -}}
{{- $en := required (printf "each engine in box %q needs a name" $bn) $e.name -}}
{{- if not (regexMatch $labelRe $en) }}{{- fail (printf "engine name %q in box %q is not a valid DNS-1123 label" $en $bn) }}{{- end -}}
{{- if has $en $engNames }}{{- fail (printf "engine name %q is used more than once in box %q" $en $bn) }}{{- end -}}
{{- $engNames = append $engNames $en -}}
{{- $p := required (printf "engine %q in box %q needs a remotePort" $en $bn) $e.remotePort -}}
{{- if has (toString $p) $ports }}{{- fail (printf "remotePort %v (engine %q/%q) is used more than once — every engine and exporter remotePort must be unique across all boxes" $p $bn $en) }}{{- end -}}
{{- $ports = append $ports (toString $p) -}}
{{- $svc := include "taas.tunnelEngineService" (dict "root" $root "box" $bn "engine" $en) -}}
{{- if gt (len $svc) 63 }}{{- fail (printf "Service name %q is %d chars (>63 limit) — shorten the release name, box %q, or engine %q" $svc (len $svc) $bn $en) }}{{- end -}}
{{- end -}}
{{- $expNames := list -}}
{{- range $x := ($box.exporters | default list) -}}
{{- $xn := required (printf "each exporter in box %q needs a name" $bn) $x.name -}}
{{- if not (regexMatch $labelRe $xn) }}{{- fail (printf "exporter name %q in box %q is not a valid DNS-1123 label" $xn $bn) }}{{- end -}}
{{- if has $xn $expNames }}{{- fail (printf "exporter name %q is used more than once in box %q" $xn $bn) }}{{- end -}}
{{- $expNames = append $expNames $xn -}}
{{- $xp := required (printf "exporter %q in box %q needs a remotePort" $xn $bn) $x.remotePort -}}
{{- if has (toString $xp) $ports }}{{- fail (printf "remotePort %v (exporter %q/%q) is used more than once — every engine and exporter remotePort must be unique across all boxes" $xp $bn $xn) }}{{- end -}}
{{- $ports = append $ports (toString $xp) -}}
{{- $_ := required (printf "exporter %q in box %q needs an in-cluster `port`" $xn $bn) $x.port -}}
{{- $xsvc := include "taas.tunnelExporterService" (dict "root" $root "box" $bn "exporter" $xn) -}}
{{- if gt (len $xsvc) 63 }}{{- fail (printf "Service name %q is %d chars (>63 limit) — shorten the release name, box %q, or exporter %q" $xsvc (len $xsvc) $bn $xn) }}{{- end -}}
{{- end -}}
{{- end -}}
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
