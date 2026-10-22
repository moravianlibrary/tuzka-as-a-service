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
# To record external CPU + idle flame graphs and stage timings around the same run:
make perf-profile TARGET=k3d USERS=50 RATE=10 DURATION=2m
```

`perf-profile` attaches py-spy from an ephemeral debug container, so the profiled
application keeps its own memory limit. It saves the SVGs and stage logs under the
chosen `OUT` directory (default `bench/perf_out/<timestamp>/`). A plain `perf-taas`
run avoids profiler overhead when comparing throughput after a fix.

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
| Harvest cost: fetch outputs concurrently, one multi-row result insert, pipelined `set_done` without the `HGETALL` | `4608429`, `539d2f0`, `f2f0a1d` | 10b: 13.22 done/s, `store_s` p95 9.30 s; 50b: 10.02 done/s | 10b: 15.28 done/s, 7.64 s; 50b: 11.75 done/s with 5 % more offered load |

## Where the time goes (plan 11 B3)

Measured on the same k3d laptop and worker limits as the baseline, with B1 stage timers
(`fc7ae38`), py-spy 0.4.2 attached from debug containers, and no in-process profiler.
The commands were `OUT=bench/perf_out/b3-10b make perf-profile TARGET=k3d USERS=50
RATE=10 DURATION=2m` with 10 fake backends and `OUT=bench/perf_out/b3-50b make
perf-profile TARGET=k3d USERS=100 RATE=10 DURATION=2m` with 50. Each records 60 s of
on-CPU samples followed by 60 s with `--idle` for the API, submit worker, and poller.
`bench/perf_profile.sh` also captures `kubectl logs --since-time` stage lines and queries
`taas.live_metrics` for `worker_stage_seconds`. Raw logs, Locust CSV, Redis counters and
ClickHouse output are in those gitignored `bench/perf_out/` directories. Done rates and
`store_s` below use a separate ClickHouse query of `taas.job_events`:
`SELECT count(), quantile(0.95)(store_s) WHERE event='done' AND ts >= toDateTime(start)
AND ts < toDateTime(start+120)`, with `start` from the first timestamp in each
`locust_stats_history.csv` (1792667310 and 1792667775).

| Measurement | 10 backends | 50 backends | Source |
|---|---:|---:|---|
| DB jobs added (includes contract seed) | 2,548 | 4,666 | `perf_taas.log`, jobs count in `bench/perf_taas.sh` |
| POST failures | 0 | 0 | `locust_failures.csv` |
| Submit requests/s | 21.32 | 38.91 | `locust_stats.csv`, POST row |
| Done events in Locust's 120 s window/s | 1,248 / 120 = 10.40 | 1,081 / 120 = 9.01 | ClickHouse window query above |
| `store_s` p95 | 12.08 s | 59.97 s | ClickHouse window query above |
| Redis commands/DB job added | 52.44 | 36.36 | Sum of `redis_per_job.txt` from `bench/perf_taas.sh` |

These are profiling runs, not a before/after optimisation comparison. The done rate
counts jobs completed during the two-minute window; queued jobs continued to drain after
Locust stopped. The historical unprofiled baseline above was about 19 and 18 done/s.

The tables below use the last two 60-second stage flushes in each worker log, excluding
the first partial idle flush in the 10-backend run. `Calls/s` is the summed count divided
by 120; p95 is the larger of the two per-flush p95 values. `Share` divides each stage's
summed wall seconds by the sum of the **listed** stages. These are overlapping task
durations, not exclusive process time: concurrent waits and nested stages can add to
far more than 120 seconds. The shares locate accumulated waiting, not CPU occupancy.

Submit worker, from `worker-submit-stages.log` (`stage=… n=… total_s=… p95_s=…`):

| Stage | 10b share | 10b calls/s | 10b p95 | 50b share | 50b calls/s | 50b p95 |
|---|---:|---:|---:|---:|---:|---:|
| backend_state | 16.8% | 136.50 | 0.003 s | 0.4% | 61.25 | 0.005 s |
| dispatch.image_get | 8.5% | 9.95 | 0.063 s | 31.6% | 7.82 | 6.883 s |
| dispatch.engine_submit | 28.0% | 9.95 | 0.099 s | 29.4% | 7.82 | 3.503 s |
| dispatch.set_running | 26.7% | 9.95 | 0.101 s | 18.2% | 7.82 | 2.607 s |
| dispatch.db_update | 20.1% | 9.95 | 0.090 s | 20.5% | 7.82 | 4.407 s |

Poller, from `worker-poller-stages.log` (same calculation):

| Stage | 10b share | 10b calls/s | 10b p95 | 50b share | 50b calls/s | 50b p95 |
|---|---:|---:|---:|---:|---:|---:|
| harvest_wait | 78.9% | 9.98 | 5.096 s | 86.0% | 7.71 | 35.005 s |
| poll.poll_wait | 2.5% | 9.98 | 0.512 s | 9.7% | 7.71 | 3.987 s |
| fetch_outputs | 5.3% | 9.98 | 0.300 s | 1.4% | 7.71 | 0.394 s |
| set_done | 5.6% | 9.98 | 0.299 s | 1.2% | 7.71 | 0.311 s |
| db_write | 4.1% | 29.95 | 0.098 s | 0.8% | 23.12 | 0.111 s |
| store_put | 3.5% | 19.97 | 0.103 s | 0.8% | 15.42 | 0.124 s |
| inflight_scan | 0.1% | 0.38 | 0.210 s | <0.1% | 0.53 | 0.936 s |

On-CPU flame graphs: [10b API](perf_taas/b3-10b/api-cpu.svg),
[submit](perf_taas/b3-10b/worker-submit-cpu.svg),
[poller](perf_taas/b3-10b/worker-poller-cpu.svg);
[50b API](perf_taas/b3-50b/api-cpu.svg),
[submit](perf_taas/b3-50b/worker-submit-cpu.svg),
[poller](perf_taas/b3-50b/worker-poller-cpu.svg).
`--idle` graphs: [10b API](perf_taas/b3-10b/api-idle.svg),
[submit](perf_taas/b3-10b/worker-submit-idle.svg),
[poller](perf_taas/b3-10b/worker-poller-idle.svg);
[50b API](perf_taas/b3-50b/api-idle.svg),
[submit](perf_taas/b3-50b/worker-submit-idle.svg),
[poller](perf_taas/b3-50b/worker-poller-idle.svg).

The SVG `<title>` sample counts show the submit worker's two backend Redis reads at
about 8.7% and 7.9% of its 10b on-CPU samples; in the 50b graph, `dispatch_job` and
the engine submit path dominate. The poller's `fetch_outputs` path accounts for about
12.7% (10b) and 9.9% (50b) of its on-CPU samples; status checking is about 8–9%.
These are inclusive stack samples, so parent and child percentages overlap. `--idle`
mostly exposes idle thread and event-loop stacks and cannot assign `await` time to a
coroutine; the stage timers provide that missing wall-time view.

Ranked step C work from these measurements:

1. **C4, harvest cost.** `harvest_wait` p95 rises from 5.10 to 35.01 s and holds the
   poller behind its ten harvest slots. Investigate `set_done`, output fetch/store and
   DB writes first. Compression totals only 1.50 and 1.14 s, so moving compression to
   a thread is not supported by this profile.
2. **C1, submit backend reads.** At 10 backends `backend_state` is 16.8% of the listed
   submit stage time; `GET` is 8.87 commands per submitted job. Batch the reads.
3. **C3, poller job metadata.** `HGETALL` is 9.36 commands per submitted job at 10b;
   caching unchanged in-flight metadata should reduce Redis traffic, although the scan
   itself is a small share of timed wall work.
4. **C5, tracing overhead.** Redis and HTTP tracing wrappers appear in the CPU graphs;
   measure sampled tracing before changing defaults.
5. **C2, first poll timing.** Polling takes about 8–9% of poller on-CPU samples, but the
   harvest queue is the larger limit under these loads. Test the fast-first-poll idea
   after the harvest path is shortened.

### C4: harvest cost

B3 ranked the harvest path first, so C4 (plan 11) changed only what its stage timers
showed: fetch the requested outputs at once, write their result rows in one statement,
and mark the job done without re-reading the job hash.

| Change | Commit | 10b stage mean, B3 → C4 | 50b stage mean, B3 → C4 |
|---|---|---:|---:|
| Fetch a job's outputs concurrently | `4608429` | `fetch_outputs` 143 → 122 ms/job | 221 → 173 ms/job |
| One multi-row `INSERT … ON CONFLICT` for the results, one job update | `539d2f0` | `db_write` 112 → 64 ms/job (3 → 2 round trips) | 124 → 83 ms/job |
| Pipelined `set_done` with the job hash passed in, no `HGETALL` | `f2f0a1d` | `set_done` 150 → 53 ms/job | 188 → 72 ms/job |

The "before" means are the last 60 s flush of the B3 profiling runs
(`bench/perf_out/b3-10b/worker-poller-stages.log`, `b3-50b/…`, py-spy attached); the
"after" means are the full flushes of the unprofiled C4 runs at the same backend counts.
The end-to-end numbers below come from a back-to-back A/B of the two worker images
(`taas-worker:b1-stages` and `taas-worker:c4-fetch`), which is the only fair comparison
on this noisy host.

10 fake backends, 50 Locust users (`OUT=bench/perf_out/c4-ab-b1-10b` and
`c4-ab-c4-10b`, `make perf-taas TARGET=k3d USERS=50 RATE=10 DURATION=2m`):

| | before `b1-stages` | after `c4-fetch` |
|---|---:|---:|
| jobs done in 120 s | 1,587 (13.22/s) | 1,833 (15.28/s) |
| `queue_s` p50 / p95 | 17.60 / 31.23 s | 9.74 / 26.37 s |
| `store_s` p50 / p95 | 4.99 / 9.30 s | 4.44 / 7.64 s |
| submit → done p50 / p95 | 23.82 / 37.52 s | 15.35 / 30.48 s |
| POSTs / failed | 2,510 / 0 | 2,509 / 1 (status 0 at the ramp) |

50 fake backends, 100 users (`c4-ab-b1-50b` and `c4-ab-c4-50b`, same command with
`USERS=100`):

| | before `b1-stages` | after `c4-fetch` |
|---|---:|---:|
| jobs done in 120 s | 1,202 (10.02/s) | 1,410 (11.75/s) |
| `queue_s` p50 / p95 | 10.68 / 42.23 s | 7.25 / 56.50 s |
| `store_s` p50 / p95 | 21.08 / 50.52 s | 21.25 / 53.32 s |
| submit → done p50 / p95 | 34.73 / 76.27 s | 33.55 / 78.27 s |
| POSTs / failed | 4,023 / 0 | 4,221 / 0 |

The 10b run is the clean comparison: both runs offered the same ~2,500 jobs, and the
after run completed 15 % more of them with an 18 % lower `store_s` p95. At 50 backends
the poller is behind a queue whose p95 is 42–56 s, and the closed-loop Locust test
hands the freed capacity straight back as more submissions: the after run offered 5 %
more POSTs, so its queue and per-job latencies are higher even though it completed 17 %
more jobs.
Per-job latency at 50b is therefore not comparable between the two columns; the done
rate is.

The same unchanged `b1-stages` code measured 16.27 done/s at 10b earlier that afternoon
and 13.22/s twenty-five minutes later, and 14.07/s at 50b at 12:32 against 10.02/s at
13:18, so single runs more than ~20 minutes apart are not comparable on this host. The
A/B pairs above were run back to back with the image swapped in between, and are the
numbers to quote. Raw output: `bench/perf_out/c4-ab-*`, `c4-before-*`, `c4-after-*`
(gitignored).

The D4 targets remain missed in these profiling runs: 10b done rate is 10.40/s,
50b is lower at 9.01/s, 10b Redis use is 52.44 commands/job, and `store_s` p95 is
12.08 s. Re-run `perf-taas` without attached profilers after each fix for comparable
before/after numbers.

After the 50b jobs drained, `/load` still showed 17 queued although Redis had zero
pending, delayed and in-flight IDs (`redis-cli ZCARD jobs:pending:0`, `ZCARD
jobs:delayed`, `SCARD jobs:inflight`). The existing `rebuild_load_counters` function
recounted them to zero; the cleanup worker also runs that recount on its heavy sweep.
