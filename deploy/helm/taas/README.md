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

## In-cluster TuzkaOCR engines

Define one entry per engine under `ocrEngines`; each is deployed (its own Deployment +
Service) and auto-registered as a taas backend by a post-install hook. `ocrEnginesDefaults`
holds the shared config; each entry needs a `name` and may override any key (deep-merged):

```yaml
ocrEngines:
  - name: cpu-1
  - name: cpu-2
    maxInflight: 4
    env: { TUZKAOCR_LINE_WORKERS: "2", TUZKAOCR_MAX_QUEUE: "4" }
    resources: { limits: { cpu: "4" } }
```

An **empty `ocrEngines`** list deploys no engines — register external backends (e.g. a remote
GPU box) yourself via `POST /admin/backends`.

Each engine is one pod behind its own Service, so a job's submit and its status/result polls
always hit the same pod (the engine keeps job state locally). **Scaling:** add/remove list
entries and `helm upgrade`. Scale-down removes the Deployment/Service but does **not**
deregister the backend — the orphan fails its health check (skipped by the submit worker) and
any in-flight job is failed by the reaper; disable it via the dashboard/API to tidy up.

**Tuning** (`ocrEnginesDefaults`): the recognizer is single-line, so use `OCR_THREADS=1` and
parallelize at the line level (see `bench/DEFAULTS.md`). Defaults target a ~1-CPU scale-out
engine: `LINE_WORKERS=1`, `PAGE_WORKERS=2` (overlap I/O), `maxInflight=8`. Keep
`TUZKAOCR_MAX_QUEUE == maxInflight` (and both ≥ `PAGE_WORKERS`) so taas never overflows the
engine's queue. Scale throughput by adding engines, not threads.

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

### OCR engines

| Key | Default | Description |
|---|---|---|
| `ocrEngines` | `[]` | List of engines to deploy + register. Empty = none. Each item needs a `name`; may override any `ocrEnginesDefaults` key |
| `ocrEnginesDefaults.image.repository` / `.tag` | `tuzkaocr` / `cpu` | Engine image |
| `ocrEnginesDefaults.maxInflight` | `8` | Backend concurrency at registration (keep `== TUZKAOCR_MAX_QUEUE`) |
| `ocrEnginesDefaults.env` | (TUZKAOCR_*) | Engine tuning env (`OCR_THREADS=1`, `LINE_WORKERS=1`, `PAGE_WORKERS=2`, `MAX_QUEUE=8`) |
| `ocrEnginesDefaults.storage.{results,spool}` | memory `128Mi`/`256Mi` | Scratch volumes (`memory`/`emptyDir`/`pvc`) |
| `ocrEnginesDefaults.resources` | req 0.5 CPU / limit 2 CPU | Per-engine resources |

### App tunables (`config.*`)

Non-secret `app/config.Settings` fields — `allowedExtensions`, `maxUploadBytes`, worker ticks
(`submitTickSeconds`, `pollerTickSeconds`, `pollerHarvestConcurrency`, `pollBackoff*`),
`zstdCompressionLevel`, `rateLimit*`, `wsCatchUpSeconds`. Job timeouts, retention, and the
presigned-URL TTL are runtime config in the DB `config` table (see the dashboard / `PUT /admin/config`),
not Helm values.

### Secrets (`secrets.*`)

`masterKey`, `keyEncryptionSecret` (Fernet), `postgresPassword`, `minio{Incoming,Results}{Access,Secret}Key`,
`ocrEngineApiKey`. **Override all for non-dev deployments**, ideally from an external secret manager.

### Exposure (`expose.<api|legacy|adminDashboard>.*`)

`kind` (`ingress`|`gateway`|`none`), `host`, `paths` / `pathPrefix`, `ingressClassName`,
`annotations`, `tls`, `gateway.{name,namespace,sectionName}`.
