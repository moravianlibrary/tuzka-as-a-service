# taas performance (plan 10 Part B)

Measures **taas**, not OCR: the backends are fake engines (`bench/fake_engine/`) with
near-zero latency, so every second here is taas' own overhead. One real-engine run at the
end checks the real path didn't regress (`bench/api`, `RESULTS_API.md`).

## How to run

Docker Compose:

```bash
docker compose -f docker-compose.yml -f bench/fake_engine/compose.yaml up -d --build
# register the fake engines as backends (bench/fake_engine/README.md), then:
make perf-taas USERS=50 DURATION=2m          # -> bench/perf_out/<timestamp>/
```

k3d (what the baseline below used; ClickStack on, so `queue_s` / `store_s` come back too):

```bash
docker build -t fake-tuzkaocr:local bench/fake_engine && k3d image import fake-tuzkaocr:local -c taas
helm upgrade taas deploy/helm/taas --reuse-values -f bench/fake_engine/values.k3d.yaml
# disable every non-fake backend (taas-admin backend disable <id>), wait 30 s, then:
make perf-taas TARGET=k3d USERS=50 RATE=10 DURATION=2m
```

Profiles: py-spy isn't in the images, so attach it from an ephemeral container while the
load runs, then copy the SVG out:

```bash
kubectl debug <pod> --image=python:3.12-slim --target=<container> --profile=sysadmin \
  --container=pyspy -- sh -c "pip install -q py-spy; sleep 3600"
kubectl exec <pod> -c pyspy -- py-spy record -d 60 -r 50 -o /tmp/p.svg --pid 1
kubectl cp -c pyspy <pod>:/tmp/p.svg p.svg
```

## Baseline

2026-10-21, `test/integration-0.9`, k3d on a laptop (12 cores, 14 GB, one node). One
replica each of the API (1 CPU limit), submit worker, poller and cleanup worker (500m CPU
limit each, `deploy/local/values.local.yaml`). Fake engines: `FAKE_LATENCY_S=0.05`,
`FAKE_CAPACITY=8`, `FAKE_WORKERS=8`, taas `max_inflight` 8. Locust: `tests/load/locustfile.py`
(submit 5 : status 3 : list 1), 2 min.

| | 10 backends, 50 users | 50 backends, 100 users |
|---|---|---|
| commit | `47f31d2` | `1195421` |
| submit p50 / p95 (ms) | 27 / 60 | 40 / 190 |
| status p50 / p95 (ms) | 10 / 21 | 13 / 75 |
| submits/s, jobs done/s | 21.3 / ~19 | 42.1 / ~18 |
| `queue_s` p50 / p95 (s) | 2.4 / 6.2 | 12.8 / 45.1 |
| `store_s` p50 / p95 (s) | 3.6 / 5.4 | 18.1 / 32.0 |
| submit → done p50 / p95 (s) | 6.3 / 10.4 | 35.2 / 66.0 |
| Redis commands per job | 75.6 | 53.4 |
| CPU avg (API / submit / poller) | 426m / 275m / 392m | 515m / 380m / 358m (both workers peak at 500m) |
| SQL time, all statements | 3.3 s in 2 min | — |

Commands are in `bench/perf_taas.sh`; raw output in `bench/perf_out/baseline-50u/` and
`baseline-50b-100u/` (not committed).

**10 000 pages as batches** (5 × 2000-page `.tar`, submitted at once, outputs
`alto,txt`, 10 backends, `8312f83`+): every page done, 0 failed. Upload and split 47 s
(~210 pages/s); all finished in 568 s, results archives ready 8 s after each batch;
**17.6 pages/s**. Batches finish one after another (140 s … 568 s), not side by side:
pages are dispatched strictly by submit time. (A single 10 000-page batch is over
`BATCH_MAX_PAGES`, 2000.)

**Top SQL** (10 backends, total time in 2 min): the `job_results` upsert 782 ms (4658
calls), `INSERT INTO jobs` 503 ms, the dispatch `UPDATE jobs` 466 ms, `SELECT count(*) FROM
jobs WHERE username` 444 ms (562 calls, 0.79 ms each: the `GET /jobs` total, the only one
that grows with the table). Postgres isn't a limit here.

**Redis per job** (10 backends): `HGETALL` 14.6 (the poller rereads every in-flight job
hash each pass), `GET` 11.8 (the submit worker reads each backend's in-flight counter
each pass), `HMGET` 5.9, `EXPIRE` 5.7, `HSET` 5.6. The first two scale with passes ×
backends or in-flight jobs, not with jobs: at 50 backends `GET` is 21 per job.

Flame graphs (10 backends, 50 users, 60 s, on-CPU samples):
[api](perf_taas/baseline/api.svg), [submit](perf_taas/baseline/worker-submit.svg),
[poller](perf_taas/baseline/worker-poller.svg).

## What limits it

1. **The poller, at ~20 jobs/s per 500m.** Every job costs a status poll (often more
   than one) and a harvest: fetch each output, compress, store, upsert, events. Both
   saturate one poller at about 20 jobs/s whatever the number of backends; with 400 jobs
   in flight (50 backends), polls crowd out harvests and `store_s` goes from 3.6 to 18 s.
2. **Slots are held until harvest**, so (1) also caps dispatch: a 0.05 s job keeps its
   backend slot ~4 s (first poll after `POLL_BACKOFF_INITIAL` = 1 s, then a harvest
   slot). Dispatch passes run every ~70 ms but find 2 free slots each; `queue_s` is
   waiting for slots, not for the submit worker.
3. **Per-backend round trips in the submit worker**: `get_backend_inflight` and
   `get_backend_batches` (2 Redis calls per backend per pass, each an OTel span) take ~26 %
   of its CPU at 10 backends, and grow with the backend count.
4. **Instrumentation on every Redis call and HTTP request** (`opentelemetry` wrappers in
   all three profiles). Worth measuring with tracing sampled down.

The API is not the limit: submit p95 stays under 200 ms at 42 submits/s on one replica.

## Targets

Proposed from this baseline (plan 10 D4), same hardware and limits:

- 10 backends: **≥ 40 jobs done/s** (2×), `store_s` p95 ≤ 2 s.
- 50 backends: no worse than 10 (today it's 5× worse on latency).
- Redis commands per job ≤ 40.

## Fixes

One row per fix (T5): what, commit, before → after.

| Fix | Commit | Before | After |
|---|---|---|---|
| Poll fan-out bounded (`POLLER_POLL_CONCURRENCY`) and a blocking Redis pool: the poller crash-looped at 400 jobs in flight (`MaxConnectionsError`, OOM) | `1195421` | 50 backends: 107 jobs done in 2 min, poller restarting | 2161 done, no restarts |
| Orphan requeue checked and requeued atomically (found by the batch run: 23 jobs dispatched twice) | `8312f83` | 23 double dispatches in 10 000 pages | 0 |
| Backend in-flight counters rebuilt by the cleanup worker (they had drifted to -5…+2 after those double dispatches) | `b10dd36` | 8 backends off | 0 |
