# Local end-to-end test: taas on k3d + real host engines via the tunnel

Runs the **whole app** (api, workers, compat, Redis, MinIO, Postgres) on a local
**k3d** cluster, with **two real TuzkaOCR engines (GPU + CPU) running on your host**,
surfaced inside the cluster through the in-cluster `frps` reverse-tunnel proxy — no app
changes, the engines look like ordinary in-cluster backends. The two engines are
registered as two backends with different priorities so you can test **heterogeneous
dispatch + prioritization**.

```
host                                            k3d cluster (taas)
 engine-proxy/ (docker compose)
   tuzkaocr-gpu :8000 ◄─ reverse tunnel ─┐
   tuzkaocr-cpu :8000 ◄─ reverse tunnel ─┤
   frpc ── dials localhost:32700 ────────┼──►  taas-frps  (NodePort 32700)
                                  ├─► taas-tunnel-engine-gpu1:8000  (priority 10) ─┐
                                  └─► taas-tunnel-engine-cpu1:8000  (priority 0) ──┴► workers
```

Backends are registered by the Helm hook via an idempotent `PUT /admin/backends`
(`managed=true`), so changing `priority`/`device`/`maxInflight` in `values.local.yaml`
and re-deploying **updates** them in place. The dashboard shows a **`deploy`** badge on
managed backends (manual edits to those are reverted on the next deploy).

## Prereqs

`docker`, `kubectl`, `helm`, and `k3d` (the script prints an install hint if missing).
The CloudNativePG operator is installed for you by the script.

The whole lifecycle is driven from the **Makefile** — every target shares the
`local-deploy-` prefix:

| Target | Does |
|---|---|
| `make local-deploy-build-cpu` | Build the engine image `tuzkaocr:local-cpu` from `TuzkaOCR/Dockerfile` |
| `make local-deploy-build-gpu` | Build the engine image `tuzkaocr:local-gpu` from `TuzkaOCR/Dockerfile.gpu` |
| `make local-deploy-up` | `setup.sh` (k3d + chart) **+** start host engine + tunnel |
| `make local-deploy-forward-up` | Start the port-forwards (background) + open the UIs |
| `make local-deploy-forward-down` | Stop the background port-forwards |
| `make local-deploy-down` | Stop engine + uninstall chart, **keep** the k3d cluster |
| `make local-deploy-clean` | Stop engine + chart **and delete** the k3d cluster |
| `make local-deploy-seed` | Seed 3 users (alice/bob/carol) with priorities 10/5/0 + fixed keys |
| `make local-deploy-scenarios` | Run the prioritization test scenarios |

(`make local-deploy-build` builds both engine images at once.)

## 1. Build the engine image (once)

```sh
make local-deploy-build-cpu      # or: make local-deploy-build-gpu
```

Built from the local `TuzkaOCR/` checkout (never pulled), so the engine matches the
repo's code. The GPU variant needs `nvidia-container-toolkit` on the host; for GPU,
also set `OCR_IMAGE=tuzkaocr:local-gpu` and `OCR_DEVICE=cuda` in
`deploy/local/engine-proxy/.env`.

## 2. Bring up the stack + engine

```sh
make local-deploy-up
```

This runs `setup.sh` — creates the `taas` k3d cluster (mapping host `:32700` → the frps
NodePort), installs CloudNativePG, builds + imports the `taas-{api,worker,compat}`
images, installs the chart, waits for the register hook — then starts the host engine +
`frpc`. Within a few seconds the `taas-tunnel-engine-cpu1` Service answers.

## 3. Port-forward the services + open the UIs

```sh
make local-deploy-forward-up      # background; stop with: make local-deploy-forward-down
```

This starts all three port-forwards detached (PIDs tracked in
`$TMPDIR/taas-local-forward.pids`), waits for the API, and opens the dashboard and
Swagger docs via `xdg-open`. (Override ports with `API_PORT`/`COMPAT_PORT`/`RESULTS_PORT`
env vars.)

| URL | What | Auth |
|---|---|---|
| `http://localhost:8080/` | Dashboard UI (redirects to `/dashboard`) | master key |
| `http://localhost:8080/docs` | Swagger / OpenAPI explorer | none |
| `http://localhost:8080/api/v1/...` | Job API (submit/status/results) | user API key |
| `http://localhost:8080/admin/...` | Admin API (users, backends, config) | `X-Master-Key: test-master-key` |
| `http://localhost:8080/dashboard/...` | Dashboard data endpoints | `X-Master-Key: test-master-key` |
| `ws://localhost:8080/ws` | Job result push | user API key |
| `http://localhost:8081` | Legacy PERO/ProArc compat API | per compat config |
| `http://localhost:9100` | MinIO results bucket (presigned downloads) | presigned URL |

The master key for this local stack is **`test-master-key`** (from `values.local.yaml`);
the dashboard UI prompts for it. The results forward on **9100** matters: presigned
result URLs are SigV4-signed for `http://localhost:9100` (set via
`minio.results.publicUrl`), so the host client can actually download them.

Prefer to do it by hand? Run each forward yourself (separate terminals or `&`):

```sh
kubectl port-forward svc/taas-api 8080:8000
kubectl port-forward svc/taas-compat 8081:8001
kubectl port-forward svc/taas-minio-results 9100:9000
```

## Verify

**Tunnel is live** (engine reachable from inside the cluster):

```sh
kubectl run curl --rm -it --image=curlimages/curl --restart=Never -- \
  curl -sf http://taas-tunnel-engine-cpu1:8000/healthz && echo OK
```

**Backend registered** (auto-registered by the Helm hook):

```sh
curl -s http://localhost:8080/admin/backends -H "X-Master-Key: test-master-key" | python -m json.tool
```

**Create a test user** and grab its API key:

```sh
curl -s -X POST http://localhost:8080/admin/users \
  -H "X-Master-Key: test-master-key" -H "Content-Type: application/json" \
  -d '{"username":"testuser"}' | python -m json.tool
```

**Submit a real job** through the public API (uses the bundled Python client +
sample images in `test-data/`):

```sh
python scripts/test-client-python.py http://localhost:8080 <API_KEY> test-data
# ALTO/TXT written to ./output/ — produced by the engine on your host, via the tunnel.
```

Watch it flow: `kubectl logs -f deploy/taas-worker-submit` and `...-worker-poller`,
and `docker compose -f deploy/local/engine-proxy/compose.yaml logs -f frpc tuzkaocr`.

## Test users + prioritization scenarios

```sh
make local-deploy-seed         # alice (prio 10), bob (5), carol (0) — fixed API keys
make local-deploy-scenarios    # runs the two scenarios below (N=6 jobs/burst; override N=)
```

`seed` is idempotent — it sets deterministic keys (`test-key-alice`, …) via
`PUT /admin/users/{name}/key` and priorities via `PATCH`, writing them to
`deploy/local/.users.env` (gitignored). The two prioritization axes:

- **Backend priority** — the submit worker orders backends by `priority desc`, so jobs
  prefer `gpu1` (priority 10) until its `max_inflight`, then spill to `cpu1` (0).
  Scenario A submits a burst as one user and prints the per-backend job split.
- **User priority** — a job is enqueued into `jobs:pending:{user.priority}`, and the
  queue drains higher-priority lists first. Scenario B queues a low-priority (carol)
  burst, then a high-priority (alice) burst, and shows alice's jobs start first.

Both engines must be up + healthy for jobs to drain; watch the **Backends** and
**Analytics** dashboard tabs while the scenarios run.

## Troubleshooting

- **Job never dispatches / backend unreachable** — frps/frpc auth mismatch. Confirm
  `FRP_TOKEN` (.env) == `secrets.frpToken` and `OCR_API_KEY` == `secrets.ocrEngineApiKey`
  in `values.local.yaml`. Failures surface in `frpc` (host) and `taas-frps` (cluster)
  logs, NOT in k8s endpoints — the tunnel-engine Service is just a port-alias.
- **frpc can't reach the server** — check host `:32700` maps to the cluster:
  `kubectl get svc taas-frps` and `k3d cluster list`. The mapping is created at
  cluster-create time; if you made the cluster by hand, recreate with
  `-p "32700:32700@server:0"`.
- **Result download fails from the host** — ensure the 9100 port-forward is running.

## Teardown

```sh
make local-deploy-down    # stop engine, uninstall chart, delete PVCs; KEEP cluster
make local-deploy-clean   # also delete the k3d cluster (full removal)
```

Keeping the cluster (`down`) lets you redeploy fast with `make local-deploy-up`, since
the k3d cluster and the CloudNativePG operator are already in place.
