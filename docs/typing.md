# Typing — 2026-10-25

Type everything that can be typed before splitting up the oversized functions
(`refactor/complexity`), so the refactor builds on typed values instead of raw dicts
and tuples. This is the agreed layout and the checklist; tick items off as they land.

## Rules

- **Pydantic for anything parsed or validated** — a boundary: HTTP, Redis, a JSONB
  column, the config table, an engine's HTTP response, env (pydantic-settings).
- **Frozen dataclasses for internal values** that nothing parses. They live next to the
  code that owns them; a type moves to a shared module only when a second package uses it.
- **SQLAlchemy entities over column tuples.** Select the mapped model for row reads;
  raw SQL (`text()`) stays where the ORM fights the query (lateral unnest, `FOR UPDATE`),
  but its rows come back as a typed model, not `Row`.
- **`StrEnum` for any vocabulary used in more than one module.** `Literal` only for a
  single local parameter. DB columns map the enum onto the existing text column + CHECK
  (non-native), so no migration. Outputs and domains are not enums: engines and DB rows
  define them at run time.

## Layout

```
app/
  vocabulary.py         the shared StrEnums
  config.py             Settings (env), including OTEL_* / HYPERDX_*
  schemas/              pydantic, by boundary
    api/                HTTP request/response: job, batch, backend, user, domain
    redis.py            JobState (job hash), BatchCounts, HarvestTimes
    runtime_config.py   RuntimeConfig, RateLimits (the config table)
    attempt.py          Attempt (Job.attempts JSONB)
  models/               SQLAlchemy only; types.py holds the pydantic JSONB TypeDecorator
  engines/
    base.py             EngineAdapter protocol + internal results (dataclasses)
    tuzkaocr/ tuzkam/   adapter.py + schemas.py (that engine's HTTP responses, private)
    cache.py            backend → adapter cache shared by the workers
  services/             logic; owns its internal dataclasses (JobEvent, LoadTotals, …)
  workers/poller/ …     after the refactor: worker, steps, types.py (no tuple lists)
compat/app/schemas.py   the compat service's own Redis state
```

`schemas` and `models` import only `vocabulary` from `app`; everything else may import
all three. `app/core/` goes away: its schemas move to `schemas/api/`, `admin_ops.py` to
`services/`. Vertical feature modules are a separate, later decision.

## Checklist

### 0. Vocabulary

- [ ] `JobStatus` (queued, running, storing, done, failed) — ~100 literals in 16 files
- [ ] `BatchStatus` (open, queued, finished)
- [ ] `EngineState` (queued, running, done, failed, error, unknown); `PollStatus` adds
      unreachable; `PollAction` (harvest, requeue, fail, running)
- [ ] `Stage` (dispatch, poll, engine, harvest, timeout — the `Literal` lacks timeout)
- [ ] `EventName`, `ErrorClass` (from `Literal`s in `services/events.py`)
- [ ] `Device` (cpu, gpu): schema `Literal`, `Backend.device: str`
- [ ] `AdapterId` (tuzkaocr, tuzkam): `ADAPTERS` keys, the default repeated 8×
- [ ] `Fmt` (alto, txt, multi): two `Literal`s in `routers/jobs.py`
- [ ] `LimitClass` (submit, query, ws_connect); `LoadGroupBy` (domain, adapter)

### 1. Schemas in one place

- [ ] D8: `app/schemas/` + `app/core/schemas/` → `app/schemas/api/`; `admin_ops` → services

### 2. Boundaries

- [ ] B1 + D1: `JobState` parsed once in `redis_jobs`; replaces three hand-built job
      dicts (`routers/jobs.py`, `routers/batches.py` ×2), `job_domain`/`job_outputs`/
      `job_options`/`parse_avoid`, the ~60 raw `meta[...]` reads, `PendingJob`'s parsing
- [ ] D2: one rate-limit model instead of `UserLimitOverrides`/`EffectiveLimits`/
      `ClassLimits` + `FALLBACK_LIMITS`/`USER_OVERRIDE_COLUMNS`
- [ ] B2: `RuntimeConfig` instead of ~15 `get_*` + `int()` + default getters
- [ ] B4 + D6: `Attempt` for `Job.attempts` (built in `retry_policy`, read in
      `events`, `poller`)
- [ ] B3: response models per engine endpoint; `resp.json()` no longer `dict[str, Any]`
- [ ] B5: `OTEL_*` (telemetry) and `HYPERDX_*` (import_dashboard) into Settings
- [ ] B6: compat `CompatState.get_request` returns a model
- [ ] B7 + D5: CLI rows from `schemas.api.*.model_validate`; drop the `COLUMNS` tuples
      (the user CLI's columns already drifted from `UserList`)

### 3. Shared internals

- [ ] D3: one backend → adapter cache (`poller.py` and `submit.py` each have one)
- [ ] D4: `Backend.display_label` instead of `label or str(id)` ×4
- [ ] D7: `JobEvent` built from `JobState` instead of field by field in four places
- [ ] Named types for the tuples: `process_chunk`'s done/failed/requeue lists,
      `results_to_store`, `perf.Scores`, `StageTimer` totals, `peek_pending`,
      `live_metrics` backends, batch `pages`, `job_spec._capabilities`

### 4. SQLAlchemy

- [ ] Entity selects in `domain_sync._domain_rows`, `get_domain_limits`
- [ ] Typed rows for `_SERVED_DOMAINS_SQL` and `batch_store`'s raw SQL

### 5. Function refactors (the `noqa: C901` list)

- [ ] poller `main`, `harvest`, `process_chunk`, `drain_poll_queue`
- [ ] submit `main`, `dispatch_job`
- [ ] cleanup `main`
- [ ] `routers/batches.py` `submit_batch`
- [ ] `deploy/box/scheduler/schedule.py` `run`

## Left as is

- `clients/python` models: a published client without a pydantic dependency.
- Cleanup worker constants (`DELETE_BATCH_SIZE`, …): fixed tuning, not per-deploy config.
- `ArchiveKind`, the dashboard importer's `Action`: single-use `Literal`s.
