# Bench: TUZKAOCR_PAGE_WORKERS ∈ {1, 2, 3}

**Date:** 2026-06-13
**Status:** design (approved knobs) — not committed to git (per repo convention: no committing plans)

## Question

For the in-cluster TuzkaOCR CPU engine configured as in `deploy/helm/taas/values.yaml`,
does changing `TUZKAOCR_PAGE_WORKERS` from 2 to 1 or 3:

1. change **per-task running time** — `finished_at − started_at` as recorded in the taas DB, and
2. **hurt throughput** (pages/sec drained from a saturated queue),

when the engine is held to the deploy's **2-core** ceiling?

## Why this matters

`page_workers` → `JobStore(ThreadPoolExecutor(max_workers=page_workers))`
(`TuzkaOCR/tuzkaocr/jobs.py:37`, wired in `api/app.py:158`). It is the number of pages
OCR'd concurrently. With `OCR_THREADS=1`/`LINE_WORKERS=1` each page is single-threaded;
ONNX releases the GIL during inference, so workers run across cores. On a 2-core box,
`pw=2` saturates and `pw=3` oversubscribes — the realistic risk we're testing.
`ENGINE_MEMORY.md` already shows peak RSS ≈ `0.1 GiB + pw × ~1.3 GiB`; this bench adds the
latency/throughput axis.

## Setup

Driven through the **already-running docker-compose stack** (api + workers + engine + redis +
postgres + minio). No in-process or standalone-server harness.

**Engine env — aligned to `values.yaml`, overriding current compose defaults:**

| var | bench value | (current compose) |
|---|---|---|
| `TUZKAOCR_DEVICE` | `cpu` | cpu |
| `TUZKAOCR_OCR_THREADS` | `1` | 1 |
| `TUZKAOCR_LINE_WORKERS` | `1` | **4** |
| `TUZKAOCR_MAX_QUEUE` | `4` | **16** |
| `TUZKAOCR_CPU_MEM_ARENA` | `false` | false |
| `TUZKAOCR_PAGE_WORKERS` | **1 / 2 / 3 (swept)** | 2 |

**CPU ceiling:** add `cpuset: "0,1"` to the `ocr-engine` service → exactly 2 cores
(matches `values.yaml` `cpu: "2"`).

**Backend dispatch cap:** registered backend `max_inflight = 4` (already seeded, matches
values.yaml). taas dispatches up to 4 concurrent to the one engine; only `pw` run at once,
the remainder wait in the engine's internal queue — that wait is where `page_workers` shows up.

These edits go in `docker-compose.yml` (engine env is inline there, not in `.env`).
`PAGE_WORKERS` is parameterized as `${BENCH_PAGE_WORKERS:-2}` so the harness sets it per run
and recreates only the engine via `docker compose up -d ocr-engine` (seconds; no full down/up).

## Workload

- **Corpus:** 60 pages sampled from `test-data/*_image.jpeg` (100 available), same 60 every run.
- **Rounds:** 3, **interleaved** — pw order rotated per round (Latin-square, like
  `bench_matrix_clean.py`) to control thermal/run-order drift. Report **medians** across rounds.
- **Submission:** via `clients/python/taas_client` (`TaasClient.submit`), fired with a few
  parallel submitters so the queue stays saturated for the whole run. A fresh bench user is
  created per harness invocation to obtain an API key (keys are only returned at creation).
- **Drain:** wait until every submitted job reaches `done`/`failed` (poll the DB / client).

## Timing semantics (taas DB `jobs` table)

- `submitted_at` — job created in taas.
- `started_at` — submit worker dispatches to engine (`app/workers/submit.py:129`).
- `finished_at` — poller observes engine `done` (`app/workers/poller.py:173`).

`finished_at − started_at` = engine-side (queue + compute) + up to ~1 s poller lag. Consistent
across all pw settings, and is literally the "time between finished and started" requested.
`app/services/stats.py:60` already computes this expression.

## Metrics, per pw (median over 3 rounds)

- **Running time** `finished_at − started_at`: median / p95 / max.
- **Throughput**: `N_done ÷ wall`, where wall = `max(finished_at) − min(started_at)`. pages/sec.
- **taas queue wait** `started_at − submitted_at`: median (completeness).
- **Health**: done / failed counts, and **requeues** (`jobs.requeues`) — oversubscription can
  drive the engine to `503` when MAX_QUEUE=4 fills, causing requeues that depress throughput.
- **Engine peak RSS**: sampled from `docker stats` during each run (cross-check vs ENGINE_MEMORY.md).

DB queried via `docker compose exec -T postgres psql`, filtered `WHERE submitted_at >= <run_start>`
to isolate each run's jobs.

## Output

- `bench/RESULTS_PAGE_WORKERS.md` — summary tables (running time, throughput, health, RSS) + a
  short verdict on whether pw=2 is the sweet spot vs 1/3 under the 2-core ceiling.
- Raw per-job rows kept as TSV alongside.

## Cleanup

After the bench, revert `docker-compose.yml` (engine env + `cpuset` + the `BENCH_PAGE_WORKERS`
parameterization) to its original state and `docker compose up -d ocr-engine` so the running
stack is left exactly as found.

## Non-goals

- Not changing TuzkaOCR engine code (the taas DB already records started/finished).
- Not measuring GPU, other thread knobs, or accuracy/parity (covered by existing benches).
- Not a production load test — single engine replica, fixed 60-page corpus.
