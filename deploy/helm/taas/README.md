# taas Helm chart

Deploys the full [taas](../../../README.md) stack on Kubernetes: API, workers
(submit/poller/cleanup), compat server, Redis, PostgreSQL (CloudNativePG), two MinIO
instances, and optionally one or more in-cluster engine StatefulSets. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the topology and request flow.

## Prerequisites

- Kubernetes 1.26+ and Helm 3/4
- [CloudNativePG operator](https://cloudnative-pg.io/) installed in the cluster
- Container images for `taas-api`, `taas-worker`, `taas-compat` (and every enabled
  `engines.*` image) in a reachable registry (`image.registry` / `image.tag`)
- For `expose.*.kind: gateway`: the [Gateway API](https://gateway-api.sigs.k8s.io/) CRDs
  and an existing `Gateway`

## Install

```bash
helm install taas ./deploy/helm/taas \
  --set image.registry=registry.example.com \
  --set image.tag=0.5.0 \
  --set secrets.keyEncryptionSecret=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  --set secrets.postgresPassword=$(openssl rand -hex 16) \
  --set secrets.minioIncomingSecretKey=$(openssl rand -hex 16) \
  --set secrets.minioResultsSecretKey=$(openssl rand -hex 16)
```

Post-install hooks create the MinIO buckets and run `alembic upgrade head`.

## In-cluster engines

`engines` is a map keyed by engine name. Every enabled entry becomes a **StatefulSet**
plus a headless Service, an autoscaler (or a static replica count), and one backend per
ordinal. TuzkaOCR (`engines.ocr-engine`) and TuzkaM (`engines.tuzkam`) are just two
entries; add another key (for example `ocr-gpu`) to run a second image or hardware class
in the same release.

Each pod has stable per-ordinal DNS
(`<release>-<engine>-<i>.<release>-<engine>`), so a job's submit and its status/result
polls always hit the same pod (the engine keeps job state locally). A post-install hook
pre-registers **one backend per ordinal** `0 .. maxReplicas-1` (`0 .. replicas-1` for
`mode: none`); ordinals that aren't running yet fail their health check and are skipped by
the submit worker until the autoscaler brings them up.

```yaml
engines:
  ocr-engine:                     # -> <release>-ocr-engine StatefulSet/Service
    enabled: true
    adapter: tuzkaocr             # used by taas-admin backend upsert
    image:
      repository: registry.example.com/tuzkaocr
      tag: "1.3.0"
    port: 8000
    maxInflight: 2                # taas dispatch cap PER POD
    auth:
      env: TUZKAOCR_API_KEY       # env var the engine image reads
      generate: true              # dev: create <release>-ocr-engine-api-key
      # existingSecret:           # prod: point at your own Secret instead
      #   name: taas-ocr-engine-key
      #   key: api-key
      # generate: false            # required when using existingSecret
    autoscaling:
      mode: hpa                   # none | hpa | keda
      replicas: 1                 # only for mode: none
      minReplicas: 1
      maxReplicas: 16             # also the number of backends pre-registered
  tuzkam:
    enabled: true
    adapter: tuzkam
    image: { repository: tuzkam, tag: "europe-med" }
    maxInflight: 1
    auth:
      env: TUZKAM_API_KEY
      generate: true
    autoscaling:
      mode: keda
      minReplicas: 0              # KEDA can scale to zero
      maxReplicas: 4
```

**Scaling:** `hpa` scales on CPU; `keda` scales on the queued work the API reports at
`GET /load` (`autoscaling.keda.*`, needs `secrets.loadToken`; see `AUTOSCALING.md`); `none`
uses a static `autoscaling.replicas` (default 1). Raising `maxReplicas` later needs a
`helm upgrade` so the register hook adds the new ordinals' backends. Under a GitOps
controller (Argo CD / Flux) the autoscaler needs a `/spec/replicas` ignore rule or scaled-up
pods get reverted and killed — see [AUTOSCALING.md](AUTOSCALING.md).

**Engine shape:** `env` is a map, `volumes` is a map of volume name → `mountPath` plus
`emptyDir` (with optional `medium`/`sizeLimit`) or `persistentVolumeClaim`; `resources`,
`podSecurityContext`, `nodeSelector`, `tolerations` and `affinity` are passed through.
`declaredDomains` registers capabilities before the first pod has run, which is what lets
KEDA scale from zero.

**API keys** are per engine and least-privilege. An engine's `auth.env` is the variable
its image reads; the value comes from either `auth.existingSecret.{name,key}` (recommended
for production, e.g. External Secrets/SealedSecrets/Vault) or `auth.generate: true`, which
creates a dedicated `<release>-<engine>-api-key` Secret and keeps its key across upgrades
via `lookup`. Generated keys are not reproducible with `helm template`, so don't use them
under Argo CD/Flux. Only the engine pod and the short-lived register hook receive the key;
it is not in `<release>-secret` or any app pod's `envFrom`. `existingSecret.name` and
`generate: true` are mutually exclusive. Omit `auth` to run an engine without a key (this
also clears a key stored on that managed backend by an earlier release). Generated keys
live in `<release>-<engine>-api-key` (read one with
`kubectl get secret <name> -o jsonpath='{.data.api-key}' | base64 -d`). The engine pod
carries a `checksum/auth` annotation, so rotating an existing Secret rolls the StatefulSet
when Helm's `lookup` can read it; under a GitOps controller that disables `lookup`, use a
reloader or restart the StatefulSet after rotating the key. When Helm can query the
cluster, it also fails the install/upgrade before applying anything if an
`existingSecret` (including the default tunnel Secret) is missing, lacks the configured
key, or has an empty value. `helm template` and lookup-disabled GitOps cannot perform
this existence check, so there the pod fails at container start instead.

When no entry is enabled, no engine is deployed — register external backends (e.g. a
remote GPU box) with `taas-admin backend upsert` (in the api pod), or reverse-tunnel an
off-cluster engine in (see the next section).

> **Breaking change in 0.9.0:** the old top-level `ocrEngine` and `tuzkam` blocks are gone.
> Move them under `engines.ocr-engine` / `engines.tuzkam`. The old
> `secrets.ocrEngineApiKey` / `secrets.tuzkamApiKey` and the interim
> `secrets.engineApiKeys` map are gone too: give each engine an `auth` block
> (see above), and tunnel engines a `tunnelBoxesDefaults.auth` / per-engine `auth`.
> `storage` becomes `volumes`; `minReplicas`/`maxReplicas` move under `autoscaling`.

**Tuning** (`engines.ocr-engine.env`): the recognizer is single-line, so use `OCR_THREADS=1`
and parallelize at the line level (see `bench/DEFAULTS.md`). Defaults target a ~1-CPU pod:
`LINE_WORKERS=1`, `PAGE_WORKERS=1`, `maxInflight=2`. Keep `TUZKAOCR_MAX_QUEUE == maxInflight`
(and both ≥ `PAGE_WORKERS`) so taas never overflows the engine's queue. Scale throughput by
adding replicas, not threads.

## Exposure (Ingress / Gateway API)

Each target is exposed independently — `kind: ingress | gateway | none`:

| Target | → Service | Paths | Rewrite |
|---|---|---|---|
| `api` | API | `/api` (jobs at `/api/v1`), `/ws` | none |
| `legacy` | compat | `/legacy` | prefix **stripped** |

```bash
helm upgrade --install taas ./deploy/helm/taas \
  --set expose.api.kind=gateway \
  --set expose.api.host=taas.example.com \
  --set expose.api.gateway.name=public-gw \
  --set expose.legacy.kind=ingress \
  --set expose.legacy.host=taas.example.com \
  --set expose.legacy.ingressClassName=nginx \
  ... (secrets as above)
```

- `gateway` emits an `HTTPRoute` referencing `expose.<t>.gateway.*`; the `legacy` prefix-strip
  uses the standard `URLRewrite` filter.
- `ingress` on `legacy` adds the ingress-nginx `rewrite-target` annotation; for other
  controllers set `expose.legacy.annotations`.

## Values reference

### Global

| Key | Default | Description |
|---|---|---|
| `image.registry` | `""` | Registry prefix for app images |
| `image.tag` | `latest` | Default image tag |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy |
| `imagePullSecrets` | `[]` | Pull secrets for private registries |

### Components

| Key | Default | Description |
|---|---|---|
| `api.replicaCount` | `1` | API replicas |
| `api.image.repository` | `taas-api` | API image |
| `api.port` | `8000` | API container port |
| `worker.image.repository` | `taas-worker` | Worker image (shared by all workers) |
| `worker.resources` | req 250m / 256Mi, limits 1 / 512Mi | Default resources for every worker |
| `workers.{submit,poller,cleanup}.replicaCount` | `1` | Replicas per worker |
| `workers.{submit,poller,cleanup}.resources` | `worker.resources` | Per-worker resource override (the poller uses req 1 / mem 256Mi, limits 2 / mem 512Mi) |
| `compat.enabled` | `true` | Deploy the legacy-compat server |
| `compat.replicaCount` | `1` | Compat replicas |
| `compat.ttlSeconds` | `"3600"` | Compat request-state TTL |

### Data layer

| Key | Default | Description |
|---|---|---|
| `redis.storage.size` | `1Gi` | Redis PVC size |
| `cnpg.name` | `taas-db` | CNPG Cluster name (DB host is `<name>-rw`) |
| `cnpg.instances` | `1` | Postgres instances |
| `cnpg.pgStatStatements` | `true` | Collect per-query stats in `pg_stat_statements` |
| `cnpg.storage.size` | `10Gi` | Postgres PVC size |
| `cnpg.database` / `cnpg.owner` | `taas` / `taas` | DB name / owner |
| `minio.incoming.bucket` | `incoming` | Upload bucket |
| `minio.results.bucket` | `results` | Results bucket |
| `minio.{incoming,results}.storage.size` | `20Gi` | MinIO PVC sizes |

### ClickStack (`clickstack.*`)

ClickHouse for job events (one row per job state change, `taas.job_events`; the schema
in `files/clickstack/schema.sql` is applied by a post-install/upgrade Job), HyperDX for
dashboards, traces and logs, and MongoDB for HyperDX's own state. Off by default.

**First use.** HyperDX has no users after the install. The first person to sign up
creates the team, which gets the chart's ClickHouse connection and sources
(`files/clickstack/sources.json`: Job Events, Live Metrics, Logs, Traces). The
dashboards (`files/clickstack/dashboard.json`) are imported through HyperDX's API,
which takes a user's API key: copy it from HyperDX's Team Settings into
`secrets.hyperdxApiKey` and upgrade the release. Until then the import Job logs that
it's skipping and the deploy succeeds; afterwards every upgrade re-imports the
dashboards (updated in place, by name).

| Key | Default | Description |
|---|---|---|
| `clickstack.enabled` | `false` | Deploy ClickStack; the api and workers then emit job events to it |
| `clickstack.clickhouse.user` | `taas` | ClickHouse user (password: `secrets.clickhousePassword`) |
| `clickstack.clickhouse.storage.size` | `50Gi` | ClickHouse PVC size |
| `clickstack.clickhouse.threads.*` | see values | Thread-pool caps for the 2-CPU ClickHouse pod; the defaults create ~100 threads instead of ClickHouse 24.8's ~700 |
| `clickstack.hyperdx.image.tag` | `latest` | Pin a release for production |
| `clickstack.mongodb.storage.size` | `1Gi` | MongoDB PVC size |
| `clickstack.otel.enabled` | `true` | OTel collector for the app's traces and logs; sets `OTEL_EXPORTER_OTLP_ENDPOINT` on the app pods, tail-samples traces and stores logs at WARN+ |
| `clickstack.otel.retentionDays` | `30` | TTL of `otel_traces` / `otel_logs` |
| `clickstack.otel.traceSamplePercentage` | `5` | Collector tail sampling for non-error traces; traces with an error span or failed job are always kept |
| `clickstack.otel.traceDecisionWait` | `10m` | How long the collector waits for a trace's spans; must exceed the longest job trace |
| `clickstack.otel.traceNumTraces` | `30000` | Max traces held in memory for tail sampling |
| `clickstack.otel.resourceAttributes` | `""` | Extra `OTEL_RESOURCE_ATTRIBUTES` for the app |
| `expose.hyperdx.*` | `kind: none` | The HyperDX UI, on its own host |

### Engines (`engines.*`)

`engines` is a map keyed by engine name (a DNS-1123 label). The key becomes the
StatefulSet/Service name suffix and the backend label prefix, so keep it short. Every
enabled entry gets the same generic shape; `ocr-engine` and `tuzkam` are the shipped
defaults.

| Key | Default | Description |
|---|---|---|
| `engines.<name>.enabled` | `false` | Deploy this StatefulSet, Service, autoscaler and per-ordinal backend registration |
| `engines.<name>.adapter` | — | `taas` adapter name used for registration (`tuzkaocr`, `tuzkam`, …) |
| `engines.<name>.image.repository` / `.tag` | — / `image.tag` | Engine image |
| `engines.<name>.image.registry` | `""` | Optional registry prefix for this image |
| `engines.<name>.port` | `8000` | Engine HTTP port |
| `engines.<name>.maxInflight` | `1` | Backend concurrency **per pod** at registration |
| `engines.<name>.auth.env` | `""` | Env var the engine image reads for its API key; required when `auth` is set |
| `engines.<name>.auth.existingSecret.{name,key}` | `""` / `api-key` | Existing Secret (same namespace) holding the key; recommended for production. Helm fails fast if it is missing, lacks the key, or the key is empty (when cluster lookup is available) |
| `engines.<name>.auth.generate` | `true` for the shipped `ocr-engine`/`tuzkam` entries | Create `<release>-<engine>-api-key` (dev; not reproducible with `helm template`); set `false` when using `existingSecret` |
| `engines.<name>.env` | `{}` | Extra engine env vars (string map) |
| `engines.<name>.volumes` | `{}` | Map of volume name → `{mountPath, emptyDir | persistentVolumeClaim, readOnly?}` |
| `engines.<name>.resources` | `{}` | Pod resources |
| `engines.<name>.podSecurityContext` / `.securityContext` | `{}` | Pod and container security contexts |
| `engines.<name>.nodeSelector` / `.tolerations` / `.affinity` | empty | Scheduling overrides for this engine |
| `engines.<name>.terminationGracePeriodSeconds` | `120` | Drain time; TuzkaM uses `600` |
| `engines.<name>.readinessInitialDelaySeconds` / `.livenessInitialDelaySeconds` | `30` / `60` | Probe delays; path is `/healthz` |
| `engines.<name>.autoscaling.mode` | `none` | `none` (static `replicas`) / `hpa` (CPU) / `keda` (queued work from `GET /load`) |
| `engines.<name>.autoscaling.replicas` | `1` | Static replicas; only for `mode: none` |
| `engines.<name>.autoscaling.{minReplicas,maxReplicas}` | `1` / `1` | Autoscaler bounds; `maxReplicas` = number of backends pre-registered |
| `engines.<name>.autoscaling.hpa.*` | 70% / 300s | HPA CPU target + scale-down stabilization window |
| `engines.<name>.autoscaling.keda.*` | — | KEDA `metrics-api` trigger (`loadFilter`, `targetBacklogSeconds`, `activationBacklogSeconds`, `pollingInterval`, `cooldownPeriod`) when `mode: keda` |
| `engines.<name>.declaredDomains` | `{}` | Capabilities registered before the engine has run (needed to scale from zero) |

#### TuzkaOCR (`engines.ocr-engine`)

Text OCR, default `mode: hpa`, `minReplicas: 1`, `maxReplicas: 16`, `maxInflight: 2`.
Its `declaredDomains` are `default`, `kramarky`, `handwritten` and `kurrent` (outputs
`alto`, `txt`). The default request is 1 CPU / 1500Mi with a 2Gi limit (Guaranteed CPU),
and the env targets a ~1-CPU pod; keep `TUZKAOCR_MAX_QUEUE == maxInflight`.

#### TuzkaM (`engines.tuzkam`)

TuzkaM serves the `maps` domain (outputs `json`, `geojson`, `gcp`, `alto`, `txt`). It
defaults to `mode: keda` with `minReplicas: 0` and `maxReplicas: 4`, `maxInflight: 1`,
2 CPU / 4Gi request and 8Gi limit, so it needs the KEDA operator and `secrets.loadToken`.
Every ordinal up to `maxReplicas` is registered with the maps capabilities declared, so
map jobs queue while no pod runs.

Set `jobs.queued_timeout_seconds` to at least twice TuzkaM's cold start (image pull +
model load), or map jobs submitted at zero replicas time out before the first pod is
ready. Measured on k3d with the image already on the node: 35–44 s from submit to
dispatch, so the 900 s default leaves room for a first image pull (~1.7 GB) too.

### Off-cluster boxes via reverse tunnel (`tunnel.*`, `tunnelBoxes`)

For a GPU box that can dial out but accepts no inbound, run the engine(s) off-cluster and
reverse-tunnel them in with FRP. The chart deploys an `frps` server and exposes each tunnel
engine as a `<release>-tunnel-engine-<box>-<engine>` Service registered like any backend.
One box can run N engines but has at most one cAdvisor + one GPU exporter, so engines and
exporters are grouped per box; each exporter becomes a `<release>-tunnel-box-<box>-<exporter>`
Service (a Prometheus scrape target). Box-side setup: [`deploy/box/`](../../box/README.md).

| Key | Default | Description |
|---|---|---|
| `tunnel.enabled` | `false` | Deploy the in-cluster `frps` server. Required when `tunnelBoxes` is set |
| `tunnel.image.repository` / `.tag` | `snowdreamtech/frps` / `0.61.1` | frps image |
| `tunnel.controlPort` | `7000` | frps control (bind) port inside the pod |
| `tunnel.service.type` | `NodePort` | `NodePort` (bare metal) or `LoadBalancer` (cloud / MetalLB) — the box dials this |
| `tunnel.service.nodePort` | `32700` | Port the box dials (`<node-ip>:<nodePort>`); ignored for `LoadBalancer` |
| `tunnelBoxes` | `[]` | Off-cluster boxes. Each: `name`, `engines[]` (`name` + `remotePort`), optional `exporters[]` (`name` + `remotePort` + `port`). All `remotePort`s unique across all boxes |
| `tunnelBoxesDefaults.{port,maxInflight,device}` | `8000` / `8` / `cpu` | Per-engine defaults (overridable per engine); backend priority is admin-managed (`taas-admin backend update`) |
| `tunnelBoxesDefaults.auth.existingSecret.{name,key}` | `""` / `api-key` | Key the box engine expects. `name: ""` means `<release>-tunnel-engine-key`. Only `existingSecret` is allowed: the box must share the value, so there is no `generate`; Helm fails fast when the Secret/key is missing or empty (when cluster lookup is available). Omit for a no-key engine |
| `metrics.serviceMonitor.enabled` | `false` | Emit a Prometheus-Operator `ServiceMonitor` per box exporter (else scrape the exporter Services statically) |
| `secrets.frpToken` | `replaceMe` | Shared secret authenticating frpc↔frps (== box `FRP_TOKEN`) |

Without the Prometheus Operator, scrape the exporter Services directly:

```yaml
- job_name: taas-cadvisor
  static_configs:
    - targets: ['taas-tunnel-box-box1-cadvisor:8080']
- job_name: taas-gpu-exporter
  static_configs:
    - targets: ['taas-tunnel-box-box1-gpu-exporter:9835']
```

### App tunables (`config.*`)

Non-secret `app/config.Settings` fields — `allowedExtensions`, `maxUploadBytes`, worker ticks
(`submitTickSeconds`, `pollerTickSeconds`, `pollerHarvestConcurrency`, `pollBackoff*`),
`zstdCompressionLevel`, `wsCatchUpSeconds`, `logLevel`. Job timeouts and the presigned-URL TTL
are runtime config in the DB `config` table (set with `taas-admin config set`), not Helm
values.

**Two config sources, no overlap.** Settings (env/Helm) carry *infrastructure* and process
tuning; the DB `config` table carries *runtime policy* (`jobs.*` timeouts, `presigned.ttl_minutes`,
`storage.*_ttl_minutes`, `rate_limit.*`). No key is read from both, so an env var and a DB value
can never disagree. Env-only knobs (e.g. `zstdCompressionLevel`, `wsCatchUpSeconds`, `logLevel`)
are deliberately not DB-tunable. **Job-record retention is hardcoded to 30 days** in the cleanup
worker — it is neither a Helm value nor a DB config key.

### Secrets (`secrets.*`)

`loadToken` (for `GET /load`; required by any `engines.<name>.autoscaling.mode=keda`),
`keyEncryptionSecret` (Fernet), `postgresPassword`, `minio{Incoming,Results}{Access,Secret}Key`;
with `clickstack.enabled`, also `clickhousePassword` (and, once someone signed up in
HyperDX, `hyperdxApiKey` for the dashboard import). **Override all for non-dev
deployments**, ideally from an external secret manager. Engine keys are deliberately
not here: see `engines.<name>.auth` and `tunnelBoxesDefaults.auth`.

### Exposure (`expose.<api|legacy|minioResults|hyperdx>.*`)

`kind` (`ingress`|`gateway`|`none`), `host`, `paths` / `pathPrefix`, `ingressClassName`,
`annotations`, `tls`, `gateway.{name,namespace,sectionName}`.
