# Job lifecycle timestamps — record when a job *actually* starts

**Date:** 2026-06-13
**Status:** design (approved) — not committed to git (repo convention: no committing plans)

## Problem

Today taas records only `submitted_at`, `started_at`, `finished_at`, and these conflate
distinct phases:

- `started_at` is set when the **submit worker dispatches** to the engine
  (`app/workers/submit.py:129`) — *before* the engine actually begins OCR. A job can sit
  in the engine's internal queue (bounded by `TUZKAOCR_MAX_QUEUE`) with the clock already
  running.
- `finished_at` is set when the **poller observes** completion at harvest
  (`app/workers/poller.py:173`) — engine-finish time + up to ~1 poll tick of lag.

So `finished_at − started_at` ("running time") is inflated by in-engine queue wait and poll
lag, and the moment a job *actually* starts processing is never captured — it exists only
inside the engine, which doesn't timestamp it (`TuzkaOCR/tuzkaocr/jobs.py` `Job` has
`created_at`/`finished_at`, no `started_at`; `_run` flips status to "running" without
recording when).

## Goal

Capture the true per-phase lifecycle so "running time" = pure OCR compute.

## Lifecycle model (five timestamps, one meaning each)

| column | set when | written by | clock |
|---|---|---|---|
| `submitted_at` | job created in taas | API | taas |
| `dispatched_at` *(new)* | submit worker POSTs job to the engine | `submit.py` | taas |
| `started_at` *(repurposed)* | engine page-worker **begins OCR** | engine → poller | engine |
| `finished_at` *(now engine-authoritative)* | engine **completes OCR** | engine → poller | engine |
| `stored_at` *(new)* | result stored in MinIO + presigned in taas | `poller.py` harvest | taas |

Derived phase durations (non-overlapping):

- **taas queue** = `dispatched_at − submitted_at`
- **engine queue** = `started_at − dispatched_at`
- **OCR running** = `finished_at − started_at`  ← the target; both from the engine clock, so
  the difference is clock-skew-free
- **harvest/store** = `stored_at − finished_at`

## Changes by component

### Engine (`TuzkaOCR`)
- `tuzkaocr/jobs.py`: add `started_at: Optional[datetime]` to `Job`; set it in `_run` at the
  `queued → running` transition (alongside the existing `finished_at` on completion).
- `api/routes.py` `get_status`: add `"started_at": job.started_at.isoformat() if
  job.started_at else None` to the `/api/v1/status/{job_id}` response (`finished_at` already
  present). Additive, backward-compatible.
- Bump engine version (1.2.1 → 1.2.2) and the image tag used by the chart/compose.

### taas poller (`app/workers/poller.py`)
- `check_one` currently returns only `(job_id, status, meta)` and discards the rest of the
  engine status dict (line 87). Thread the engine timing (`started_at`, `finished_at`) through
  to `harvest`.
- `harvest`: write engine `started_at` and engine `finished_at` (parsed from ISO) into the DB,
  plus `stored_at = datetime.utcnow()` at the point the result is stored. Handle missing engine
  timestamps gracefully (leave NULL).

### taas submit (`app/workers/submit.py`)
- At dispatch, write `dispatched_at = datetime.utcnow()` (replacing the current
  `started_at = ...`). Status still flips to `running` at dispatch.

### taas reaper (`app/services/reaper.py`)
- The "running" timeout switches its clock from `started_at` to `dispatched_at` (always set at
  dispatch, never NULL), so a job stuck in the engine queue with `started_at` still NULL is
  still reaped. Queued timeout (on `submitted_at`) unchanged.

### Migration (`alembic`)
- Add nullable `dispatched_at` and `stored_at` columns to `jobs`.
- `started_at` / `finished_at` keep their columns; semantics shift going forward. Historical
  rows retain their old values — acceptable (no backfill).

### Dashboard (`app/routers/dashboard.py`, `app/static/index.html`, `app/static/dashboard.js`)
The jobs tab already renders a table and a click-to-open detail modal (`openJobDialog`), both
fed by the single `GET /dashboard/jobs` payload (`lastJobs`).

- `dashboard.py` `get_dashboard_jobs`: add `dispatched_at` and `stored_at` to each job dict
  (alongside the existing `submitted_at`/`started_at`/`finished_at`).
- **Jobs table** (`index.html` header + `dashboard.js` row render): keep the **Submitted**
  column; replace the current **Runtime** column with **Time in system** =
  `stored_at − submitted_at`. Show `—` when `stored_at` is null (not-yet-stored / failed).
- **Job detail modal** (`openJobDialog`): show the full lifecycle — the five timestamps
  (Submitted, Dispatched, Started, Finished, Stored) **and** the derived phase durations
  (taas queue, engine queue, OCR running, harvest/store) plus total time in system. Each value
  renders `—` when its inputs are null.
- A small JS helper computes a duration between two ISO timestamps (null-safe), replacing the
  current `fmtRuntime`.

### Knock-on (no code change required, just noted)
- `app/services/stats.py` computes duration as `finished_at − started_at` — it automatically
  becomes pure OCR time (more accurate). Daily bucketing on `finished_at::date` unaffected.
- `app/routers/dashboard.py` overview `avg_duration_seconds` (`finished_at − started_at`)
  likewise becomes avg pure OCR time — more accurate; label unchanged.
- `app/workers/cleanup.py` retention uses `finished_at < cutoff`; engine-finish vs poll-observe
  differ by ~1s — negligible for a 30-day cutoff.

## Clock-skew note

`started_at` and `finished_at` are both engine-clock, so **running time is skew-free**. The
cross-clock phases (`started_at − dispatched_at`, `stored_at − finished_at`) carry whatever
node-clock skew exists; in compose (same host) it's ~0, in k8s nodes are NTP-synced — sub-ms
to low-ms, negligible against OCR durations of seconds.

## Out of scope

- Client-facing job-status API (`GET /api/v1/jobs/{id}`, schema `JobStatus`) surfacing
  `dispatched_at`/`stored_at` — low-cost follow-up, but not needed for the dashboard ask.
- Redis ephemeral state (`set_running`/`set_done`) timestamp alignment — used only for state
  TTL / WS catch-up; left as-is.

## Testing

- Engine: unit test that `_run` sets `started_at` before `finished_at` and `/status` returns it.
- taas: unit test `harvest` maps engine `started_at`/`finished_at` + sets `stored_at`; reaper
  test that a `running` job with NULL `started_at` but old `dispatched_at` is reaped.
- E2E (compose, rebuilt engine): submit a page, assert all five timestamps are populated and
  ordered `submitted ≤ dispatched ≤ started ≤ finished ≤ stored`.
- Dashboard: `GET /dashboard/jobs` includes `dispatched_at`/`stored_at`; manual check that the
  table shows "Time in system" and the detail modal shows the full lifecycle + phase durations.
