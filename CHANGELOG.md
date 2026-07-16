# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.8.0] - 2026-07-17

### Added

- The submit worker now retries transient dispatch failures instead of failing the job
  outright. A transport-level error (connect/read/write/pool, timeout) or a 5xx engine
  response requeues the job — bounded by `jobs.max_requeues` (shared with the poller's
  budget) and sidelining the offending backend for the health-cache window so the retry
  lands elsewhere. Non-transient failures (4xx, malformed responses, unexpected bugs) still
  fail fast, since retrying can't fix a bad request. Engine 503 is unchanged (EngineFull →
  unbounded requeue as backpressure).

### Fixed

- A failed dispatch (or image read) whose exception carried no message — e.g. an httpx
  transport error raised with no args — stored a blank `error`, so the dashboard's error
  box (rendered only for a truthy value) stayed hidden and the failure looked reasonless.
  The submit worker now records `str(exc) or repr(exc)`, so the exception type always
  survives to the DB and the UI.

### Changed

- The poller is now event-driven, mirroring the submit worker: `set_running` signals it
  (`signal_poll`) so a freshly in-flight job is polled at its scheduled first poll instead
  of after up to a full idle tick, and the loop now waits until the nearest `next_poll_at`
  (capped by `poller_tick_seconds`) rather than sleeping a fixed tick — cutting the
  finished→stored "harvest" latency by removing tick-quantization and backoff overshoot.
  The engine still can't push completion, so running jobs are polled on the existing
  backoff schedule; this only tightens *when* those polls fire.
- Quieted httpx's per-request `INFO` logging to `WARNING` in the API and both workers.
  At dispatch/poll volume it flooded stdout and rotated real errors (like a failed
  dispatch's traceback) out of `kubectl logs` before they could be read.

## [0.7.0] - 2026-07-16

### Security

- Master-key authentication now uses a constant-time comparison (`hmac.compare_digest`),
  removing a timing side-channel on the secret.
- An invalid or missing master key is now rejected with `401 Unauthorized` (was `403`),
  matching the documented API contract.

### Added

- Testing infrastructure: Schemathesis contract/property tests (`make test-contract`,
  run against a live instance from the OpenAPI schema) and `make coverage-report`
  (pytest-cov). The Makefile Tests group now follows the house taxonomy — `make test`
  is the unit suite (was the e2e smoke), the full-stack smoke is `make test-integration`
  (fast variant `test-integration-fast`), and `test-compat` is unchanged.
- Contract test is now runnable in one command: `make test-contract-local` brings up the
  API and its deps via compose (no workers/engine), seeds a stable fixture (API user,
  backend, one queued job) via `scripts/seed-contract.sh`, and runs Schemathesis with the
  seeded `X-API-Key`. `make test-contract` still targets an already-running/remote instance.
- Locust load test (`make load-test`, headless; `USERS`/`RATE`/`DURATION` to tune) modelling
  the submit → poll-status loop plus light reads in `tests/load/locustfile.py`. Note: without
  workers/engine it loads ingest + status/read paths, not end-to-end OCR throughput.
- Even dispatch within a priority tier: the submit worker now deals jobs round-robin
  across the healthy backends of a priority tier (highest tier first), respecting each
  backend's free capacity and served domains, instead of filling one backend to
  capacity before the next. Spreads load so results return with more parallelism.
- Event-driven dispatch: the submit worker now blocks on a Redis wakeup signal
  (`signal_submit`) raised whenever a job becomes pending or a backend slot frees, so
  fast jobs refill slots immediately instead of waiting out the fixed tick. The tick
  becomes a max idle-wait floor.
- Quality gate: `make check` (lint + `mypy --strict` types + version drift + unit tests),
  plus `make typecheck`, `make test-unit`, and `make version-check`.
- `make set-version VERSION=x.y.z` propagates the version into every manifest
  (`VERSION`, both `pyproject.toml`s, `app/main.py`, `compat/app/main.py`,
  Helm `Chart.yaml`); `make version-check` fails on drift.
- Committed `uv.lock` for reproducible installs; dev tooling runs via `uv`.
- CI: secret scanning (gitleaks) and dependency CVE audit (pip-audit). Gitleaks runs via
  the MIT-licensed CLI (not `gitleaks-action`, which now requires a paid license for org
  repos), with a `.gitleaks.toml` that allowlists the designated throwaway dev/example
  secret files (`deploy/local/values.local.yaml`, `.env.*.example`).

### Changed

- The dashboard collection endpoints (`/jobs`, `/usage`, `/analytics/breakdown`,
  `/analytics/raw`, `/facets`) now declare typed Pydantic `response_model`s instead of
  returning hand-assembled dicts, so the payloads are validated and documented in the
  OpenAPI schema.
- **API:** a failed job's result is no longer a `5xx`. `GET /jobs/{job_id}/result` now
  returns `200` with `{"status": "failed", "error": ..., "results": []}` (an engine-side
  failure is not a fault of this API), and the streaming `GET /jobs/{job_id}/result/{fmt}/download`
  returns `409` instead of `500`. Clients that polled `/result` for HTTP `500` to detect
  failure must now read the `status` field / handle `409`.
- Job submit now validates `uuid` and `fmt` at the boundary (parsed as `UUID` /
  `Literal`), so an invalid value returns `422` instead of `400`. List endpoints cap
  `limit` at 200 (`limit`/`offset` are range-validated) and the `status` filter is a
  `Literal` (invalid value → `422`). Backend `device` is validated as `cpu`/`gpu` at the
  schema boundary. The analytics CSV export is capped at 100k rows (logged if truncated).
- Index `jobs.backend_id` and add a `ck_jobs_status` CHECK constraint pinning
  `jobs.status` to the four lifecycle values (migration 010). ORM relationships now
  use `lazy="raise"`, so related rows must be loaded explicitly (no accidental N+1).
- The whole codebase now passes `mypy --strict` and an expanded Ruff rule set
  (`B` bugbear, `SIM` simplify); all `app/` and `compat/` code is fully type-annotated.
- `alembic/env.py` imports every model via the `app.models` package so
  `Base.metadata` is complete — autogenerate can no longer propose dropping the
  `domains`, `backend_domains`, or `config` tables.

### Fixed

- Admin backend/user endpoints no longer return `500` on invalid input. `POST`/`PUT`/`PATCH
  /admin/backends` and `PATCH /admin/users/{username}` accepted an empty or non-`http(s)`
  URL, strings with NUL bytes, integers outside the 32-bit column range, and an explicit
  `null` for a NOT NULL column — each of which reached the database and surfaced as a raw
  `IntegrityError`/`DataError` (`500`), and a persisted bad row then `500`ed every
  backend-listing endpoint. These are now validated at the boundary (`422`), an
  out-of-range `{backend_id}` path param is bounded (`422`), and a duplicate backend URL
  returns `409`. Surfaced by the now-seeded Schemathesis contract run (`make
  test-contract-local`).
- Re-submitting a job with an `external_id` already used by that caller no longer fails.
  `external_id` was both the object-storage key and a unique `(username, external_id)`
  constraint, so re-running OCR on the same document (same `external_id`) hit an
  unhandled `IntegrityError` (`500`) and would have overwritten the prior result. Storage
  is now keyed by the server-generated `jobs.id` (globally unique) for both incoming
  uploads and stored results, and the unique constraint is dropped (migration 011), so
  `external_id` is purely a caller-chosen correlation label that may repeat.
  **Breaking (storage layout):** objects moved from `{username}/{external_id}` to
  `{username}/{job_id}`, so results stored by an earlier version are not readable by this
  one — drain in-flight jobs before upgrading.
- A deleted/disabled user or a rotated key now stops authenticating immediately — admin
  key/active changes clear the API-key lookup cache, instead of a revoked key working for
  up to the cache TTL. HTTP and WebSocket auth now share one `authenticate_api_key` path.
- The API now fails fast at startup if `MASTER_KEY` or `KEY_ENCRYPTION_SECRET` is unset,
  instead of booting a half-configured, insecure surface.
- `get_redis` hands out a single pooled Redis client created in the app lifespan, rather
  than opening (and leaking) a new connection pool on every request.
- Worker robustness: loop errors now log full tracebacks (`logger.exception`); a failed
  domain sync retries next tick instead of being locked out; `storage.delete_objects`
  iterates delete errors correctly (was an unreachable `async for`) and logs them
  instead of `print`; fan-out dispatch/harvest no longer let one failure cancel
  siblings; workers close their engine/redis/DB clients on shutdown.
- Corrected the drifted `compat` service version (`0.5.2` → in step with the main
  version) via the new single-source version propagation.
