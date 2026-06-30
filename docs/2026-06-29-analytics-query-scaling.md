# Analytics query scaling — known limitation at large `job_analytics`

> Findings doc. Written 2026-06-29.

---

## TL;DR

The job-processing **hot path scales fine** — at 10M `job_analytics` rows the poller/submit
worker queries are all 0.04–3.7 ms. The problem is the **admin analytics/reporting
queries**: `breakdown` (~11 s), `stats.csv` yearly export (~205 s), and `stats/years`
(~33 s) at 10M rows. They are aggregation-bound (full sort + percentiles over millions
of rows) and an index cannot fix them. The eventual fix is a **pre-aggregated daily
rollup table**; not built yet because these are admin-only, rarely-run queries.

This is a known consequence of the [2026-06-18 analytics design](2026-06-18-job-analytics-design.md)
decision to use a single permanent fact table with "no rollup cron". That decision is
fine for correctness and for the fact-table writes; it just defers the reporting-scale
cost to here.

## How this was measured

`deploy/local/bench-analytics-db.sh` (`make local-deploy-bench-db`) seeds N synthetic
rows (tagged `fmt='bench'`, spread over the last 365 days, `MODE=clean` to remove) and
times the real dashboard queries **and** the worker hot-path queries via `EXPLAIN
ANALYZE`. Run on the local k3d deploy (single CloudNativePG instance), 10M rows ≈ 2.6 GB
table + ~0.9 GB indexes. Two independent 10M runs agreed within noise.

## Results at 10M rows

### Worker hot path (runs on every job) — fine

| Query | Time @ 10M |
|---|---|
| poller: `INSERT job_analytics` (1 row, ON CONFLICT, 6 indexes) | ~3.7 ms |
| poller: FK lookups (`users`/`engine_versions` by name) | 0.07–0.3 ms |
| poller: harvest read `SELECT job + backend WHERE id` | ~0.08 ms |
| submit: `UPDATE jobs … WHERE id` | ~0.5 ms |
| reaper: `SELECT jobs WHERE status IN ('queued','running')` | ~0.04 ms |

Why it's safe: `jobs` is well-indexed (PK `id`, `ix_jobs_status`, `(username,
submitted_at DESC)`) and stays small/transient; the submit worker never touches
`job_analytics`; and the only big-table write — the per-job analytics INSERT — is
O(log n), so it stays ~milliseconds as the fact table grows.

### Admin analytics queries — the problem

| Query | No `submitted_at` index | With `submitted_at` index (migration 009) |
|---|---|---|
| raw page (`ORDER BY submitted_at DESC LIMIT 51`) | ~15.5 s | **~5 ms** ✅ |
| breakdown (30-day window, group + p95) | ~9.7 s | ~11 s (not helped) |
| stats.csv (full-year aggregation + p50/p95/p99) | ~205 s | ~205 s (not helped) |
| stats/years (`DISTINCT EXTRACT(YEAR …)`) | ~33 s | ~33 s (not helped) |

- **raw page** was fixed by `ix_job_analytics_submitted_at` on `(submitted_at DESC)`
  (migration `009`): it filters/orders by `submitted_at`, but every prior index was on
  `stat_date`, so it sorted all 10M rows to return 51. Now it's a top-N index scan.
- **breakdown** is *not* helped by the index: a 30-day window is ~814k rows (8% of the
  table), so the planner picks a parallel seq scan, and the real cost is the sort +
  `PERCENTILE_CONT` aggregation over those rows — not row-finding.
- **stats.csv** / **stats/years** scan the whole table by design (full-year aggregation /
  distinct over all rows), so no index helps.

## The fix (not yet built): daily rollup table

A summary table keyed by the reporting dimensions, one row per day per combination —
e.g. `job_analytics_daily (stat_date, user_id, engine_version_id, engine_device,
domain_id, jobs_total, jobs_done, jobs_failed, ocr_sum_s, ocr_count, …)`. A year of data
is a few thousand rows instead of millions, so the dashboard reads it in milliseconds.

Population options:
1. **Incremental upsert in the poller harvest** (preferred): bump that day's counters in
   the same transaction as the fact insert — a tiny extra sub-ms write per job; reads
   stay fast forever.
2. **Materialized view / cron refresh**: simpler to add, but data is stale between
   refreshes and the refresh still scans the full table.

**Caveat — percentiles don't roll up.** `count`/`sum`/`avg`/`min`/`max` aggregate cleanly
across days; `p95`/`p99` need the raw distribution. So a rollup serves exact
counts/averages instantly, but accurate percentiles must either stay on the fact table
(bounded date range) or use an approximation (t-digest/histogram per bucket).

## Recommendation / priority

Low-to-medium priority. The hot path (job throughput) is unaffected, and these reporting
queries are admin-only and infrequent. Build the incremental `job_analytics_daily` rollup
when the analytics dashboard becomes painful in real use. Until then, mitigations:
- keep migration `009` (already done — fixes the raw page);
- keep the heavy CSV export **range-bounded** (a full-year, full-table export pins a CPU
  core for minutes);
- consider caching `stats/years` or backing it with a tiny lookup, since it is run on
  dashboard load.
