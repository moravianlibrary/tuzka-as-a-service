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

```bash
helm upgrade --install taas ./deploy/helm/taas \
  --set ocrEngine.enabled=true \
  --set secrets.ocrEngineApiKey=$(openssl rand -hex 16) \
  ... (secrets as above)
```

With `ocrEngine.enabled` and `ocrEngine.register` (default), a post-install hook registers
the engine as a backend (`POST /admin/backends`). Otherwise register external backends via
the admin API yourself.

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
| `ocrEngine.enabled` | `false` | Deploy the in-cluster TuzkaOCR engine |
| `ocrEngine.register` | `true` | Auto-register it as a backend (hook) |
| `ocrEngine.image.repository` / `.tag` | `tuzkaocr` / `cpu` | Engine image |
| `ocrEngine.maxInflight` | `4` | Backend concurrency at registration |
| `ocrEngine.env` | (TUZKAOCR_*) | Engine tuning env |
| `ocrEngine.storage.{results,spool}.size` | `10Gi` / `5Gi` | Engine PVCs |

### App tunables (`config.*`)

Non-secret `app/config.Settings` fields — `allowedExtensions`, `maxUploadBytes`, worker ticks
(`submitTickSeconds`, `pollerTickSeconds`, `pollerHarvestConcurrency`, `pollBackoff*`,
`jobTtlSeconds`), `zstdCompressionLevel`, `rateLimit*`, `wsCatchUpSeconds`, `presignedTtlMinutes`.

### Secrets (`secrets.*`)

`masterKey`, `keyEncryptionSecret` (Fernet), `postgresPassword`, `minio{Incoming,Results}{Access,Secret}Key`,
`ocrEngineApiKey`. **Override all for non-dev deployments**, ideally from an external secret manager.

### Exposure (`expose.<api|legacy|adminDashboard>.*`)

`kind` (`ingress`|`gateway`|`none`), `host`, `paths` / `pathPrefix`, `ingressClassName`,
`annotations`, `tls`, `gateway.{name,namespace,sectionName}`.
