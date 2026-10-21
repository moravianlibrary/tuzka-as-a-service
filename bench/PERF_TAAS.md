# taas performance (plan 10 Part B)

Measures **taas**, not OCR: the backends are fake engines (`bench/fake_engine/`) with
near-zero latency, so every second here is taas' own overhead. One real-engine run at the
end checks the real path didn't regress (`bench/api`, `RESULTS_API.md`).

## How to run

```bash
docker compose -f docker-compose.yml -f bench/fake_engine/compose.yaml up -d --build
# register the fake engines as backends (bench/fake_engine/README.md), then:
make perf-taas USERS=50 DURATION=2m          # -> bench/perf_out/<timestamp>/
```

While it runs, profile the api and workers (host, as root):
`py-spy record -d 60 -o OUT/<proc>.svg --pid $(docker inspect -f '{{.State.Pid}}' <container>)`.

## Baseline

_Not measured yet._ For each run record: commit, backends (count, `FAKE_LATENCY_S`,
`FAKE_CAPACITY`), Locust users/rate/duration, and:

| | value |
|---|---|
| submit p50 / p95 (ms) | |
| status p50 / p95 (ms) | |
| throughput (submits/s, pages done/s) | |
| queue → dispatch p95 (`queue_s`, s) | |
| harvest overhead p95 (`store_s`, s) | |
| Redis commands per job | |
| top 3 SQL statements by total time | |
| 10 000-page batch, end to end (s) | |

Flame graphs: link the `.svg` files.

## Targets

Set from the baseline (plan 10 D4).

## Fixes

One row per fix (T5): what, commit, before → after.

| Fix | Commit | Before | After |
|---|---|---|---|
