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
True when Helm can actually query the cluster. `lookup` silently returns an empty
result under `helm template` and under GitOps controllers that disable it, so use a
sentinel lookup to distinguish "Secret is missing" from "lookup is unavailable".
The cluster-scoped kube-system Namespace covers normal installs/upgrades; the
kube-root-ca.crt ConfigMap covers namespace-scoped credentials.
*/}}
{{- define "taas.lookupReady" -}}
{{- if or (lookup "v1" "Namespace" "" "kube-system") (lookup "v1" "ConfigMap" .Release.Namespace "kube-root-ca.crt") -}}true{{- end -}}
{{- end }}

{{/*
Normalize an engine auth block. The engine key is never rendered into the shared
<release>-secret: an engine points at an existing Secret (auth.existingSecret), or
gets a dedicated Secret the chart renders from auth.value, or (in-cluster only) a
generated one (auth.generate).

Inputs:
  root               chart root context
  name               engine name, used for the generated Secret name
  path               values path, for error messages
  auth               the raw auth map (empty => no auth)
  requireEnv         in-cluster engines need auth.env; tunnel engines ignore it
  allowGenerate      true for in-cluster engines, false for tunnel engines
  defaultSecretName  fallback Secret name when existingSecret.name is empty
                     (tunnel engines use <release>-tunnel-engine-key)

Returns a YAML map with env, secretName, secretKey, generated and value; an empty map
when auth is not configured.
*/}}
{{- define "taas.auth" -}}
{{- $root := .root -}}
{{- $path := .path -}}
{{- $auth := .auth | default dict -}}
{{- if ne (kindOf $auth) "map" -}}
{{- fail (printf "%s.auth must be a map" $path) -}}
{{- end -}}
{{- if $auth -}}
{{- $env := $auth.env | default "" -}}
{{- if and $env (not (kindIs "string" $env)) -}}
{{- fail (printf "%s.auth.env must be a string" $path) -}}
{{- end -}}
{{- if and .requireEnv (not $env) -}}
{{- fail (printf "%s.auth.env is required (the env var the engine container reads)" $path) -}}
{{- end -}}
{{- if and (not .requireEnv) $env -}}
{{- fail (printf "%s.auth.env is not used for tunnel engines; set it on the box instead" $path) -}}
{{- end -}}
{{- if $env -}}
{{- if not (regexMatch "^[A-Za-z_][A-Za-z0-9_]*$" $env) -}}
{{- fail (printf "%s.auth.env=%q is not a valid environment variable name" $path $env) -}}
{{- end -}}
{{- end -}}

{{- $existing := $auth.existingSecret | default dict -}}
{{- if ne (kindOf $existing) "map" -}}
{{- fail (printf "%s.auth.existingSecret must be a map" $path) -}}
{{- end -}}
{{- $existingName := $existing.name | default "" -}}
{{- if and $existingName (not (kindIs "string" $existingName)) -}}
{{- fail (printf "%s.auth.existingSecret.name must be a string" $path) -}}
{{- end -}}
{{- $secretKey := $existing.key | default "api-key" -}}
{{- if not (kindIs "string" $secretKey) -}}
{{- fail (printf "%s.auth.existingSecret.key must be a string" $path) -}}
{{- end -}}
{{- if not (regexMatch "^[-._a-zA-Z0-9]+$" $secretKey) -}}
{{- fail (printf "%s.auth.existingSecret.key=%q is not a valid Kubernetes Secret key" $path $secretKey) -}}
{{- end -}}
{{- $generate := $auth.generate | default false -}}
{{- if not (kindIs "bool" $generate) -}}
{{- fail (printf "%s.auth.generate must be true or false" $path) -}}
{{- end -}}
{{- if and $generate $existingName -}}
{{- fail (printf "%s.auth: set either existingSecret.name (with generate: false) or generate: true, not both" $path) -}}
{{- end -}}
{{- if and $generate (not .allowGenerate) -}}
{{- fail (printf "%s.auth: tunnel engines need existingSecret or value; the box must share the key" $path) -}}
{{- end -}}
{{- $value := $auth.value | default "" -}}
{{- if not (kindIs "string" $value) -}}
{{- fail (printf "%s.auth.value must be a string" $path) -}}
{{- end -}}
{{- if and $value (or $generate $existingName) -}}
{{- fail (printf "%s.auth: set one of existingSecret.name, generate: true and value" $path) -}}
{{- end -}}

{{- $secretName := "" -}}
{{- if $existingName -}}
{{- $secretName = $existingName -}}
{{- else if or $generate $value -}}
{{- /* A Secret the chart renders: from the value, or generated (in-cluster only). */ -}}
{{- if .allowGenerate -}}
{{- $secretName = printf "%s-%s-api-key" (include "taas.fullname" $root) .name -}}
{{- else -}}
{{- $secretName = printf "%s-tunnel-engine-%s-key" (include "taas.fullname" $root) .name -}}
{{- end -}}
{{- if gt (len $secretName) 63 -}}
{{- fail (printf "%s: generated Secret name %q is %d chars (>63); set auth.existingSecret.name to a shorter name" $path $secretName (len $secretName)) -}}
{{- end -}}
{{- else if .defaultSecretName -}}
{{- $secretName = .defaultSecretName -}}
{{- else -}}
{{- fail (printf "%s.auth: set existingSecret.name, generate: true or value" $path) -}}
{{- end -}}

{{- /*
Existing Secrets are checked before anything is applied: a typo in the name or key
should fail the release, not leave an engine pod in CreateContainerConfigError.
Generated and value-rendered Secrets are exempt: the chart creates them itself.
*/}}
{{- if and (not $generate) (not $value) $secretName (include "taas.lookupReady" $root) }}
{{- $secretObj := lookup "v1" "Secret" $root.Release.Namespace $secretName }}
{{- if not $secretObj }}
{{- fail (printf "%s.auth: existing Secret %q not found in namespace %q" $path $secretName $root.Release.Namespace) }}
{{- end }}
{{- if not (hasKey $secretObj.data $secretKey) }}
{{- fail (printf "%s.auth: existing Secret %q has no key %q" $path $secretName $secretKey) }}
{{- end }}
{{- if not (index $secretObj.data $secretKey) }}
{{- fail (printf "%s.auth: existing Secret %q key %q is empty" $path $secretName $secretKey) }}
{{- end }}
{{- end -}}

{{- toYaml (dict "env" $env "secretName" $secretName "secretKey" $secretKey "generated" $generate "value" $value) -}}
{{- else -}}
{{- toYaml (dict) -}}
{{- end -}}
{{- end }}

{{/*
Normalize and validate the named in-cluster engine pools in .Values.engines.
Each key is used as the StatefulSet/Service/backend prefix, so TuzkaOCR and TuzkaM
are just two entries (ocr-engine and tuzkam) and any number of additional engines
can be added without touching the templates.

Returns a YAML list of enabled engines with defaults filled in. Callers use:
  {{- $engines := include "taas.engines" . | fromYamlArray }}
*/}}
{{- define "taas.engines" -}}
{{- $root := . -}}
{{- $engines := .Values.engines | default dict -}}
{{- if ne (kindOf $engines) "map" -}}
{{- fail "engines must be a map keyed by engine name (e.g. engines.ocr-engine)" -}}
{{- end -}}
{{- $labelRe := "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" -}}
{{- $full := include "taas.fullname" $root -}}
{{- $out := list -}}
{{- range $name, $raw := $engines -}}
{{- if $raw.enabled -}}
{{- if not (regexMatch $labelRe $name) -}}
{{- fail (printf "engines.%s: name %q is not a valid DNS-1123 label" $name $name) -}}
{{- end -}}
{{- $resourceName := printf "%s-%s" $full $name -}}
{{- if gt (len $resourceName) 63 -}}
{{- fail (printf "engines.%s: StatefulSet/Service name %q is %d chars (>63 limit); shorten the release or engine name" $name $resourceName (len $resourceName)) -}}
{{- end -}}

{{- $adapter := required (printf "engines.%s.adapter is required" $name) $raw.adapter -}}
{{- $image := $raw.image | default dict -}}
{{- if ne (kindOf $image) "map" -}}
{{- fail (printf "engines.%s.image must be a map" $name) -}}
{{- end -}}
{{- $repository := required (printf "engines.%s.image.repository is required" $name) $image.repository -}}
{{- $tag := $image.tag | default $root.Values.image.tag | default "latest" -}}
{{- $registry := $image.registry | default "" | trimSuffix "/" -}}
{{- $imageRef := ternary (printf "%s/%s:%s" $registry $repository $tag) (printf "%s:%s" $repository $tag) (not (empty $registry)) -}}

{{- $port := 8000 -}}
{{- if hasKey $raw "port" -}}
{{- $port = int $raw.port -}}
{{- end -}}
{{- if or (lt $port 1) (gt $port 65535) -}}
{{- fail (printf "engines.%s.port must be 1-65535" $name) -}}
{{- end -}}
{{- $maxInflight := 1 -}}
{{- if hasKey $raw "maxInflight" -}}
{{- $maxInflight = int $raw.maxInflight -}}
{{- end -}}
{{- if lt $maxInflight 1 -}}
{{- fail (printf "engines.%s.maxInflight must be >= 1" $name) -}}
{{- end -}}

{{- $auth := include "taas.auth" (dict
  "root" $root
  "name" $name
  "path" (printf "engines.%s" $name)
  "auth" ($raw.auth | default dict)
  "requireEnv" true
  "allowGenerate" true
  "defaultSecretName" ""
) | fromYaml -}}

{{- $env := $raw.env | default dict -}}
{{- if ne (kindOf $env) "map" -}}
{{- fail (printf "engines.%s.env must be a map" $name) -}}
{{- end -}}
{{- if and $auth (hasKey $env $auth.env) -}}
{{- fail (printf "engines.%s.env.%s is set, but auth.env uses the same variable; remove the raw key from env and put it in a Secret" $name $auth.env) -}}
{{- end -}}
{{- $resources := $raw.resources | default dict -}}
{{- if ne (kindOf $resources) "map" -}}
{{- fail (printf "engines.%s.resources must be a map" $name) -}}
{{- end -}}
{{- $declaredDomains := $raw.declaredDomains | default dict -}}
{{- if ne (kindOf $declaredDomains) "map" -}}
{{- fail (printf "engines.%s.declaredDomains must be a map of domain -> output list" $name) -}}
{{- end -}}

{{- $volumes := $raw.volumes | default dict -}}
{{- if ne (kindOf $volumes) "map" -}}
{{- fail (printf "engines.%s.volumes must be a map keyed by volume name" $name) -}}
{{- end -}}
{{- range $volName, $vol := $volumes -}}
{{- if not (regexMatch $labelRe $volName) -}}
{{- fail (printf "engines.%s.volumes.%s: volume name is not a valid DNS-1123 label" $name $volName) -}}
{{- end -}}
{{- $_ := required (printf "engines.%s.volumes.%s.mountPath is required" $name $volName) $vol.mountPath -}}
{{- $hasEmptyDir := hasKey $vol "emptyDir" -}}
{{- $hasPVC := hasKey $vol "persistentVolumeClaim" -}}
{{- if eq $hasEmptyDir $hasPVC -}}
{{- fail (printf "engines.%s.volumes.%s: set exactly one of emptyDir or persistentVolumeClaim" $name $volName) -}}
{{- end -}}
{{- if $hasPVC -}}
{{- $_ := required (printf "engines.%s.volumes.%s.persistentVolumeClaim.claimName is required" $name $volName) $vol.persistentVolumeClaim.claimName -}}
{{- end -}}
{{- end -}}

{{- $as := $raw.autoscaling | default dict -}}
{{- if ne (kindOf $as) "map" -}}
{{- fail (printf "engines.%s.autoscaling must be a map" $name) -}}
{{- end -}}
{{- $mode := $as.mode | default "none" -}}
{{- if not (has $mode (list "none" "hpa" "keda")) -}}
{{- fail (printf "engines.%s.autoscaling.mode must be none, hpa or keda (got %q)" $name $mode) -}}
{{- end -}}
{{- $replicas := 1 -}}
{{- if hasKey $as "replicas" -}}
{{- $replicas = int $as.replicas -}}
{{- end -}}
{{- $minReplicas := 1 -}}
{{- if hasKey $as "minReplicas" -}}
{{- $minReplicas = int $as.minReplicas -}}
{{- end -}}
{{- $maxReplicas := 1 -}}
{{- if hasKey $as "maxReplicas" -}}
{{- $maxReplicas = int $as.maxReplicas -}}
{{- end -}}
{{- if eq $mode "none" -}}
{{- if lt $replicas 1 -}}
{{- fail (printf "engines.%s.autoscaling.replicas must be >= 1 for mode=none" $name) -}}
{{- end -}}
{{- $minReplicas = $replicas -}}
{{- $maxReplicas = $replicas -}}
{{- else if eq $mode "hpa" -}}
{{- if lt $minReplicas 1 -}}
{{- fail (printf "engines.%s.autoscaling.minReplicas must be >= 1 for mode=hpa (HPAs cannot scale below 1)" $name) -}}
{{- end -}}
{{- if lt $maxReplicas $minReplicas -}}
{{- fail (printf "engines.%s.autoscaling.maxReplicas must be >= minReplicas" $name) -}}
{{- end -}}
{{- else if eq $mode "keda" -}}
{{- if lt $minReplicas 0 -}}
{{- fail (printf "engines.%s.autoscaling.minReplicas must be >= 0" $name) -}}
{{- end -}}
{{- if lt $maxReplicas 1 -}}
{{- fail (printf "engines.%s.autoscaling.maxReplicas must be >= 1" $name) -}}
{{- end -}}
{{- if lt $maxReplicas $minReplicas -}}
{{- fail (printf "engines.%s.autoscaling.maxReplicas must be >= minReplicas" $name) -}}
{{- end -}}
{{- if not (or $root.Values.secrets.loadToken $root.Values.secrets.existingSecret) -}}
{{- fail (printf "engines.%s.autoscaling.mode=keda needs secrets.loadToken (or LOAD_TOKEN in secrets.existingSecret): KEDA reads the API's /load with it" $name) -}}
{{- end -}}
{{- end -}}

{{- $hpa := mergeOverwrite (dict "targetCPUUtilizationPercentage" 70 "scaleDownStabilizationSeconds" 300) ($as.hpa | default dict) -}}
{{- $hpaCPU := int ($hpa.targetCPUUtilizationPercentage | default 70) -}}
{{- if or (lt $hpaCPU 1) (gt $hpaCPU 100) -}}
{{- fail (printf "engines.%s.autoscaling.hpa.targetCPUUtilizationPercentage must be 1-100" $name) -}}
{{- end -}}
{{- $keda := mergeOverwrite (dict
  "loadFilter" (printf "adapter=%s" $adapter)
  "targetBacklogSeconds" 600
  "activationBacklogSeconds" 0
  "pollingInterval" 15
  "cooldownPeriod" 300
) ($as.keda | default dict) -}}
{{- if lt (int $keda.targetBacklogSeconds) 1 -}}
{{- fail (printf "engines.%s.autoscaling.keda.targetBacklogSeconds must be >= 1" $name) -}}
{{- end -}}
{{- if lt (int $keda.activationBacklogSeconds) 0 -}}
{{- fail (printf "engines.%s.autoscaling.keda.activationBacklogSeconds must be >= 0" $name) -}}
{{- end -}}
{{- if lt (int $keda.pollingInterval) 1 -}}
{{- fail (printf "engines.%s.autoscaling.keda.pollingInterval must be >= 1" $name) -}}
{{- end -}}
{{- if lt (int $keda.cooldownPeriod) 0 -}}
{{- fail (printf "engines.%s.autoscaling.keda.cooldownPeriod must be >= 0" $name) -}}
{{- end -}}

{{- $pmp := $raw.podManagementPolicy | default "Parallel" -}}
{{- if not (has $pmp (list "OrderedReady" "Parallel")) -}}
{{- fail (printf "engines.%s.podManagementPolicy must be OrderedReady or Parallel" $name) -}}
{{- end -}}

{{- $terminationGrace := 120 -}}
{{- if hasKey $raw "terminationGracePeriodSeconds" -}}
{{- $terminationGrace = int $raw.terminationGracePeriodSeconds -}}
{{- end -}}
{{- $readinessDelay := 30 -}}
{{- if hasKey $raw "readinessInitialDelaySeconds" -}}
{{- $readinessDelay = int $raw.readinessInitialDelaySeconds -}}
{{- end -}}
{{- $livenessDelay := 60 -}}
{{- if hasKey $raw "livenessInitialDelaySeconds" -}}
{{- $livenessDelay = int $raw.livenessInitialDelaySeconds -}}
{{- end -}}
{{- $preStopSleep := 5 -}}
{{- if hasKey $raw "preStopSleepSeconds" -}}
{{- $preStopSleep = int $raw.preStopSleepSeconds -}}
{{- end -}}

{{- $engine := dict
  "name" $name
  "adapter" $adapter
  "imageRef" $imageRef
  "port" $port
  "maxInflight" $maxInflight
  "auth" $auth
  "env" $env
  "volumes" $volumes
  "resources" $resources
  "terminationGracePeriodSeconds" $terminationGrace
  "readinessInitialDelaySeconds" $readinessDelay
  "livenessInitialDelaySeconds" $livenessDelay
  "preStopSleepSeconds" $preStopSleep
  "podManagementPolicy" $pmp
  "podSecurityContext" ($raw.podSecurityContext | default dict)
  "securityContext" ($raw.securityContext | default dict)
  "podAnnotations" ($raw.podAnnotations | default dict)
  "nodeSelector" ($raw.nodeSelector | default dict)
  "tolerations" ($raw.tolerations | default list)
  "affinity" ($raw.affinity | default dict)
  "healthPath" ($raw.healthPath | default "/healthz")
  "declaredDomains" $declaredDomains
  "device" ($raw.device | default "")
  "autoscaling" (dict
    "mode" $mode
    "replicas" $replicas
    "minReplicas" $minReplicas
    "maxReplicas" $maxReplicas
    "hpa" $hpa
    "keda" $keda
  )
-}}
{{- $out = append $out $engine -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end }}

{{/*
Build the list of exposure targets (api, minio-results, and legacy
when compat is enabled). Each entry: name, cfg, service, port, strip.
minio-results is exposed so external download clients can reach presigned result URLs
(served at root — S3 path-style puts the bucket in the path, so it must NOT be stripped).
Usage: {{- $targets := include "taas.exposeTargets" . | fromYamlArray }}
*/}}
{{- define "taas.exposeTargets" -}}
{{- $full := include "taas.fullname" . -}}
{{- $targets := list
  (dict "name" "api" "cfg" .Values.expose.api "service" (printf "%s-api" $full) "port" (.Values.api.port | int) "strip" false)
  (dict "name" "minio-results" "cfg" .Values.expose.minioResults "service" (printf "%s-minio-results" $full) "port" (.Values.minio.port | int) "strip" false)
-}}
{{- if .Values.compat.enabled -}}
{{- $targets = append $targets (dict "name" "legacy" "cfg" .Values.expose.legacy "service" (printf "%s-compat" $full) "port" (.Values.compat.port | int) "strip" true) -}}
{{- end -}}
{{- if .Values.clickstack.enabled -}}
{{- $targets = append $targets (dict "name" "hyperdx" "cfg" .Values.expose.hyperdx "service" (printf "%s-hyperdx" $full) "port" 8080 "strip" false) -}}
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

{{/*
Pod annotations that change with the app's ConfigMap and Secret, so `helm upgrade`
restarts the pods reading them (envFrom is read only at start). Call with the root
context.
*/}}
{{- define "taas.configChecksums" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
{{- if .Values.secrets.existingSecret }}
{{- /* The chart can't see an existing Secret's content: restart the pods after changing it. */}}
checksum/secret: {{ .Values.secrets.existingSecret | sha256sum }}
{{- else }}
checksum/secret: {{ include (print .Template.BasePath "/secret.yaml") . | sha256sum }}
{{- end }}
{{- end }}

{{/*
The app Secret every component reads its credentials from: secrets.existingSecret when
set (managed outside the chart), else the one the chart renders from secrets.*.
*/}}
{{- define "taas.secretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-secret" (include "taas.fullname" .)) -}}
{{- end }}

{{/*
The database owner's basic-auth Secret CloudNativePG bootstraps with:
cnpg.existingAuthSecret when set, else the one the chart renders.
*/}}
{{- define "taas.cnpgAuthSecretName" -}}
{{- .Values.cnpg.existingAuthSecret | default (printf "%s-cnpg-auth" (include "taas.fullname" .)) -}}
{{- end }}
