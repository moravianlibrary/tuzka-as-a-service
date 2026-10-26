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
    runtime_config.py   RuntimeConfig, RateLimit (the config table)
    attempt.py          Attempt (Job.attempts JSONB)
  models/               SQLAlchemy only; types.py holds the pydantic JSONB TypeDecorator
  engines/
    base.py             EngineAdapter protocol + internal results (dataclasses)
    schemas.py          the HTTP responses both engines share (job id, status)
    tuzkaocr/ tuzkam/   adapter.py + schemas.py (that engine's HTTP responses, private)
    cache.py            backend → adapter cache shared by the workers
  services/             logic; owns its internal dataclasses (JobEvent, LoadTotals, …)
  workers/poller/ …     after the refactor: worker, steps, types.py (no tuple lists)
compat/app/schemas.py   the compat service's own Redis state
```

`schemas` and `models` import only `vocabulary` from `app`, except that `models` imports
the schema of a JSONB column it types (`Job.attempts`); everything else may import all
three. `app/core/` goes away: its schemas move to `schemas/api/`, `admin_ops.py` to
`services/`. Vertical feature modules are a separate, later decision.

## Checklist

### 0. Vocabulary

- [x] `JobStatus` (queued, running, storing, done, failed) — ~100 literals in 16 files
- [x] `BatchStatus` (open, queued, finished)
- [x] `EngineState` (queued, running, done, failed, error, unknown); `PollStatus` adds
      unreachable; `PollAction` (harvest, requeue, fail, running)
- [x] `FailureStage` (dispatch, poll, engine, harvest, timeout — the `Literal` lacked timeout)
- [x] `JobEventName`, `ErrorClass` (from `Literal`s in `services/events.py`)
- [x] `Device` (cpu, gpu): schema `Literal`, `Backend.device: str`
- [x] `AdapterId` (tuzkaocr, tuzkam): `ADAPTERS` keys, the default repeated 8×
- [x] `Fmt` (alto, txt, multi): two `Literal`s in `routers/jobs.py`
- [x] `LimitClass` (submit, query, ws_connect); `LoadGroupBy` (domain, adapter)
- [x] `DomainSource` (engine, declared): `backend_domains.source`, found in the pass
- Not enums: `jobs.fmt` (a label, `custom` for any non-legacy set) and output names.
  The domain name `"default"` is repeated 6× beside `job_spec.DEFAULT_DOMAIN`; it goes
  with `JobState` (B1).

### 1. Schemas in one place

- [x] D8: `app/schemas/` + `app/core/schemas/` → `app/schemas/api/`; `admin_ops` → services

### 2. Boundaries

- [x] B1 + D1: `JobState` parsed once in `redis_jobs`; replaces three hand-built job
      dicts (`routers/jobs.py`, `routers/batches.py` ×2), `job_domain`/`job_outputs`/
      `job_options`/`parse_avoid`, the ~60 raw `meta[...]` reads, `PendingJob`'s parsing.
      Building the dicts in one place found the batch retry dropping megapixels (fixed).
- [x] B1 rest: `BatchCounts` from the batch hash; `harvest_times` as `EngineRun`; the
      poller's `next_poll_at`/`last_poll` HSET and both workers' `requeues` HINCRBY move
      into `redis_jobs`, which is now the only writer of a job's hash
- [ ] Follow-up: a pending entry whose state is gone is offered as a default job and
      dropped only once a backend claims it; drop it from its queue outright instead
- [x] D2: one rate-limit model instead of `UserLimitOverrides`/`EffectiveLimits`/
      `ClassLimits` + `FALLBACK_LIMITS`/`USER_OVERRIDE_COLUMNS`
- [x] B2: `RuntimeConfig` instead of ~15 `get_*` + `int()` + default getters
- [x] B4 + D6: `Attempt` for `Job.attempts` (built in `retry_policy`, read in
      `events`, `poller`)
- [x] B3: response models per engine endpoint; `resp.json()` no longer `dict[str, Any]`
- [x] B5: `OTEL_*` (telemetry) and `HYPERDX_*` (import_dashboard) into Settings
- [x] B6: compat `CompatState.get_request` returns a model
- [x] B7 + D5: CLI rows from `schemas.api.*.model_validate`; drop the `COLUMNS` tuples
      (the user CLI's columns already drifted from `UserList`)

### 3. Shared internals

- [x] D3: one backend → adapter cache (`poller.py` and `submit.py` each have one)
- [x] D4: `Backend.display_label` instead of `label or str(id)` ×4
- [x] D7: `JobEvent` built from `JobState` instead of field by field in four places
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
