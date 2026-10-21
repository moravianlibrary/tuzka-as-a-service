# Fake TuzkaOCR engine

A stand-in for TuzkaOCR with the same HTTP API and no model, for measuring **taas**
rather than OCR (plan 10, `docs/plans/10-performance.md` D3). Each job "runs" for a
configurable time, then returns a canned ALTO 4.4 / text result, or fails with a
configurable `error_status`. It's a few MB and a few millicores, so one machine can run
dozens of them as separate backends.

| Endpoint | Behaves like TuzkaOCR's |
|---|---|
| `GET /healthz` | `{"status": "ok"}` |
| `GET /api/v1/models` | `selectable_via_domain` + `capabilities` (`alto`, `txt` for the four domains) |
| `GET /openapi.json` | FastAPI-generated; the `process` form matches TuzkaOCR's, with `alto_profile` as an `ndk`/`basic` enum, so taas derives the same options schema |
| `POST /api/v1/process` | `{"job_id", "status": "queued"}`; **503** when `FAKE_CAPACITY` jobs are queued or running; 422 for an unknown domain |
| `GET /api/v1/status/{id}` | the same fields, incl. `error_status` for a failed job |
| `GET /api/v1/result/{id}?which=alto\|txt` | the canned result; 202 while running; the failure's status once failed |

## Configuration (env)

| Variable | Default | |
|---|---|---|
| `FAKE_LATENCY_S` | `0.05` | mean run time per page, seconds |
| `FAKE_LATENCY_DIST` | `lognormal` | `fixed`, `exp` or `lognormal` (long right tail, like real pages) |
| `FAKE_LATENCY_SIGMA` | `0.5` | lognormal spread |
| `FAKE_FAILURE_RATE` | `0` | share of jobs that fail, 0–1 |
| `FAKE_FAILURE_STATUS` | `500` | their `error_status`: 5xx is retried by taas, 4xx (e.g. `422`) isn't |
| `FAKE_CAPACITY` | `8` | queued + running jobs before `/process` answers 503 (TuzkaOCR's `MAX_QUEUE`) |
| `FAKE_WORKERS` | `2` | jobs running at once (TuzkaOCR's `PAGE_WORKERS`) |
| `FAKE_RESULT_TTL_S` | `600` | how long finished jobs are kept |
| `FAKE_SEED` | unset | seed for reproducible latency and failures |
| `TUZKAOCR_API_KEY` | unset | required `X-API-Key`, like the real engine |

Keep taas's `max_inflight` for a fake backend ≤ `FAKE_CAPACITY`, as with the real engine.

## Docker Compose

```bash
docker compose -f docker-compose.yml -f bench/fake_engine/compose.yaml up -d --build
for i in 1 2 3; do
  docker compose exec -T -e ENGINE_KEY=test-engine-key api taas-admin backend upsert \
    --url "http://fake-engine-$i:8000" --label "fake-$i" --api-key-env ENGINE_KEY \
    --max-inflight 8
done
```

Disable the real `ocr-engine` backend (`taas-admin backend disable <id>`, or don't start
it) so every job goes to the fakes.
`FAKE_*` variables in your shell or `.env` apply to all three.

## k3d

```bash
docker build -t fake-tuzkaocr:local bench/fake_engine
k3d image import fake-tuzkaocr:local -c <cluster>
helm upgrade --install taas deploy/helm/taas \
  -f deploy/local/values.local.yaml -f bench/fake_engine/values.k3d.yaml
```

`values.k3d.yaml` runs the chart's engine StatefulSet with this image: 10 replicas, each
registered as its own backend by the chart's register hook. Raise `ocrEngine.replicas`
and `autoscaling.maxReplicas` together for more.
