# taas Helm chart

Deploys the full [taas](../../../README.md) stack on Kubernetes: API, workers
(submit/poller/cleanup), compat server, Redis, PostgreSQL (CloudNativePG), two MinIO
instances, and optionally the TuzkaOCR engine. See [ARCHITECTURE.md](ARCHITECTURE.md)
for the topology and request flow.

## Prerequisites

- Kubernetes 1.26+ and Helm 3/4
- [CloudNativePG operator](https://cloudnative-pg.io/) installed in the cluster
- Container images for `taas-api`, `taas-worker`, `taas-compat` (and `tuzkaocr` if the
  in-cluster engine is enabled) in a reachable registry (`image.registry` / `image.tag`)
- For `expose.*.kind: gateway`: the [Gateway API](https://gateway-api.sigs.k8s.io/) CRDs
  and an existing `Gateway`

## Install

```bash
helm install taas ./deploy/helm/taas \
  --set image.registry=registry.example.com \
  --set image.tag=0.1.0 \
  --set secrets.masterKey=$(openssl rand -hex 16) \
  --set secrets.keyEncryptionSecret=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  --set secrets.postgresPassword=$(openssl rand -hex 16) \
  --set secrets.minioIncomingSecretKey=$(openssl rand -hex 16) \
  --set secrets.minioResultsSecretKey=$(openssl rand -hex 16)
```

Post-install hooks create the MinIO buckets and run `alembic upgrade head`.

## In-cluster TuzkaOCR engine

Set `ocrEngine.enabled: true` to deploy a single TuzkaOCR **StatefulSet** whose replica
count is driven by an autoscaler. Each pod has stable per-ordinal DNS
(`<release>-ocr-engine-<i>.<release>-ocr-engine`), so a job's submit and its status/result
polls always hit the same pod (the engine keeps job state locally). A post-install hook
pre-registers **one backend per ordinal** `0 .. maxReplicas-1`; ordinals that aren't running
yet fail their health check and are skipped by the submit worker until the autoscaler brings
them up.

```yaml
ocrEngine:
  enabled: true
  maxInflight: 4            # taas dispatch cap PER POD (keep == TUZKAOCR_MAX_QUEUE)
  autoscaling:
    mode: hpa              # none | hpa | keda
    minReplicas: 1
    maxReplicas: 8         # also the number of backends pre-registered
    hpa: { targetCPUUtilizationPercentage: 70, scaleDownStabilizationSeconds: 300 }
```

When `ocrEngine.enabled: false`, no engine is deployed — register external backends (e.g. a
remote GPU box) via `POST /admin/backends`, or reverse-tunnel an off-cluster engine in (see
the next section).

**Scaling:** `hpa` scales on CPU; `keda` scales on the Redis job-queue length
(`autoscaling.keda.*`); `none` pins the count at `minReplicas`. Raising `maxReplicas` later
needs a `helm upgrade` so the register hook adds the new ordinals' backends.

**Tuning** (`ocrEngine.env`): the recognizer is single-line, so use `OCR_THREADS=1` and
parallelize at the line level (see `bench/DEFAULTS.md`). Defaults target a ~1-CPU pod:
`LINE_WORKERS=1`, `PAGE_WORKERS=2` (overlap I/O), `maxInflight=4`. Keep
`TUZKAOCR_MAX_QUEUE == maxInflight` (and both ≥ `PAGE_WORKERS`) so taas never overflows the
engine's queue. Scale throughput by adding replicas, not threads.

## Exposure (Ingress / Gateway API)

Each target is exposed independently — `kind: ingress | gateway | none`:

| Target | → Service | Paths | Rewrite |
|---|---|---|---|
| `api` | API | `/api` (jobs at `/api/v1`), `/ws` | none |
| `legacy` | compat | `/legacy` | prefix **stripped** |
| `adminDashboard` | API | `/admin`, `/dashboard`, `/static` | none |

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
| `workers.{submit,poller,cleanup}.replicaCount` | `1` | Replicas per worker |
| `compat.enabled` | `true` | Deploy the legacy-compat server |
| `compat.replicaCount` | `1` | Compat replicas |
| `compat.ttlSeconds` | `"3600"` | Compat request-state TTL |
| `compat.engines` | (Default + Kramarky) | Engine map (JSON) |

### Data layer

| Key | Default | Description |
|---|---|---|
| `redis.storage.size` | `1Gi` | Redis PVC size |
| `cnpg.name` | `taas-db` | CNPG Cluster name (DB host is `<name>-rw`) |
| `cnpg.instances` | `1` | Postgres instances |
| `cnpg.storage.size` | `10Gi` | Postgres PVC size |
| `cnpg.database` / `cnpg.owner` | `taas` / `taas` | DB name / owner |
| `minio.incoming.bucket` | `incoming` | Upload bucket |
| `minio.results.bucket` | `results` | Results bucket |
| `minio.{incoming,results}.storage.size` | `20Gi` | MinIO PVC sizes |

### OCR engine

| Key | Default | Description |
|---|---|---|
| `ocrEngine.enabled` | `false` | Deploy the in-cluster TuzkaOCR StatefulSet + autoscaler + per-ordinal backend registration |
| `ocrEngine.image.repository` / `.tag` | `…/tuzkaocr` / `1.1.1` | Engine image |
| `ocrEngine.maxInflight` | `4` | Backend concurrency **per pod** at registration (keep `== TUZKAOCR_MAX_QUEUE`) |
| `ocrEngine.env` | (TUZKAOCR_*) | Engine tuning env (`OCR_THREADS=1`, `LINE_WORKERS=1`, `PAGE_WORKERS=2`, `MAX_QUEUE=4`) |
| `ocrEngine.storage.{results,spool}` | memory `128Mi`/`256Mi` | Scratch volumes (`memory`/`emptyDir`) |
| `ocrEngine.resources` | req 0.5 CPU / limit 2 CPU | Per-pod resources |
| `ocrEngine.autoscaling.mode` | `hpa` | `none` (pin to `minReplicas`) / `hpa` (CPU) / `keda` (Redis queue length) |
| `ocrEngine.autoscaling.{minReplicas,maxReplicas}` | `1` / `8` | Replica bounds; `maxReplicas` = number of backends pre-registered |
| `ocrEngine.autoscaling.hpa.*` | 70% / 300s | HPA CPU target + scale-down stabilization window |
| `ocrEngine.autoscaling.keda.*` | — | KEDA Redis trigger (`redisAddress`, `listName`, `listLength`) when `mode: keda` |

### Off-cluster engines via reverse tunnel (`tunnel.*`, `tunnelOcrEngines`)

For a GPU box that can dial out but accepts no inbound, run the engine off-cluster and
reverse-tunnel it in with FRP. The chart deploys an `frps` server and exposes each tunnel
engine as a `<release>-tunnel-engine-<name>` Service registered like any backend. Box-side
setup: [`deploy/gpu-box/`](../../gpu-box/README.md).

| Key | Default | Description |
|---|---|---|
| `tunnel.enabled` | `false` | Deploy the in-cluster `frps` server. Required when `tunnelOcrEngines` is set |
| `tunnel.image.repository` / `.tag` | `snowdreamtech/frps` / `0.61.1` | frps image |
| `tunnel.controlPort` | `7000` | frps control (bind) port inside the pod |
| `tunnel.service.type` | `NodePort` | `NodePort` (bare metal) or `LoadBalancer` (cloud / MetalLB) — the box dials this |
| `tunnel.service.nodePort` | `32700` | Port the box dials (`<node-ip>:<nodePort>`); ignored for `LoadBalancer` |
| `tunnelOcrEngines` | `[]` | Off-cluster engines. Each item: `name` + `remotePort` (unique); may override `tunnelOcrEnginesDefaults` |
| `tunnelOcrEnginesDefaults.port` | `8000` | Logical Service port taas registers (== engine HTTP port on the box) |
| `tunnelOcrEnginesDefaults.maxInflight` | `8` | Backend concurrency at registration |
| `secrets.frpToken` | `replaceMe` | Shared secret authenticating frpc↔frps (== box `FRP_TOKEN`) |

### App tunables (`config.*`)

Non-secret `app/config.Settings` fields — `allowedExtensions`, `maxUploadBytes`, worker ticks
(`submitTickSeconds`, `pollerTickSeconds`, `pollerHarvestConcurrency`, `pollBackoff*`),
`zstdCompressionLevel`, `wsCatchUpSeconds`, `logLevel`. Job timeouts and the presigned-URL TTL
are runtime config in the DB `config` table (see the dashboard / `PUT /admin/config`), not Helm
values.

**Two config sources, no overlap.** Settings (env/Helm) carry *infrastructure* and process
tuning; the DB `config` table carries *runtime policy* (`jobs.*` timeouts, `presigned.ttl_minutes`,
`storage.*_ttl_minutes`, `rate_limit.*`). No key is read from both, so an env var and a DB value
can never disagree. Env-only knobs (e.g. `zstdCompressionLevel`, `wsCatchUpSeconds`, `logLevel`)
are deliberately not DB-tunable. **Job-record retention is hardcoded to 30 days** in the cleanup
worker — it is neither a Helm value nor a DB config key.

### Secrets (`secrets.*`)

`masterKey`, `keyEncryptionSecret` (Fernet), `postgresPassword`, `minio{Incoming,Results}{Access,Secret}Key`,
`ocrEngineApiKey`. **Override all for non-dev deployments**, ideally from an external secret manager.

### Exposure (`expose.<api|legacy|adminDashboard>.*`)

`kind` (`ingress`|`gateway`|`none`), `host`, `paths` / `pathPrefix`, `ingressClassName`,
`annotations`, `tls`, `gateway.{name,namespace,sectionName}`.
