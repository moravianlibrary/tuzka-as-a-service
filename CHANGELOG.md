# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Batches.** `POST /api/v1/batches` takes one archive of pages (`.tar`, `.tar.gz`,
  `.tar.zst` or `.zip`, read one page at a time) with one domain, set of outputs and
  options; every image becomes a job, other files are skipped and listed. Watch it with
  `GET /api/v1/batches/{id}` (counts, ETA, `?pages=1`) or the WebSocket's
  `{"type": "batch", …}` progress events, and download every page's output plus a
  `manifest.json` as one `.tar.zst` from `GET /api/v1/batches/{id}/results`
  (`?partial=1` before it's finished). `POST /api/v1/batches/{id}/retry` reruns the
  failed pages. Limits: `BATCH_MAX_UPLOAD_BYTES` (2 GiB) and `BATCH_MAX_PAGES` (2000).
  A batch costs one submit from the rate limit. `make test-batch` runs it end to end.
- `GET /api/v1/jobs?external_id=…` finds your jobs by their label.
- **`taas-admin`**, the admin CLI, replaces the HTTP admin API and the dashboard. It ships
  in the api image and writes to Postgres directly: `user` (create, rotate-key, set-key,
  enable/disable, set-priority, set-limits, set-scopes), `backend` (upsert, update,
  enable/disable, delete), `domain` (set-label, set-legacy-id), `config` (get/set) and
  `job` (list-failed, show with attempts). Run it with
  `docker compose exec -T api taas-admin …` or `kubectl exec deploy/<release>-api -- taas-admin …`;
  `--json` for scripts; secrets come from a named env var, never argv. The Helm register
  hook and every script use it (`scripts/lib/taas-admin.sh`).
- **Scoped API keys**: `taas-admin user set-scopes <user> --domains a,b --outputs alto,txt`.
  A submit outside the key's scope is a `403`. Existing keys keep full access.
- **ClickStack analytics.** Every job state change (`submitted`, `dispatched`, `retried`,
  `done`, `failed`) is sent to ClickHouse's `taas.job_events`, with durations, OCR
  metrics and an error flag and class, but no error text (that stays in Postgres). The
  emitter is fire-and-forget: a job never fails or waits because analytics did, and
  with `CLICKHOUSE_URL` unset it's off. The Helm chart deploys ClickHouse, HyperDX and
  MongoDB behind `clickstack.enabled` (needs `secrets.clickhousePassword` and
  `secrets.hyperdxApiKey`), applies the schema on every install/upgrade, and imports
  the HyperDX dashboards from `files/clickstack/dashboard.json`. docker-compose has a
  `clickstack` profile (`make up-clickstack`). `python -m app.ops.backfill_clickstack`
  copies the history in `job_analytics` into ClickHouse once.
- **Traces and logs (OpenTelemetry).** With `OTEL_EXPORTER_OTLP_ENDPOINT` set, the api
  and workers export traces and logs over OTLP; each job is one trace from submit
  through dispatch, engine polls and harvest, across processes. Workers trace nothing
  outside a job; the API skips `/healthz` and `/load`. The chart runs an OTel collector
  into ClickHouse for HyperDX (`clickstack.otel.*`) and sets the endpoint on the pods.
- Map sheets: domain `maps`, served by the TuzkaM engine through a new `tuzkam` adapter,
  with outputs `json`, `geojson`, `gcp`, `alto` and `txt` and options `georef`, `region`,
  `alto_profile`, `source_identifier` and `physical_img_nr`. Asking for `geojson`/`gcp`
  with `georef: false` is a `422` at submit; when georeferencing fails, a job that asked
  for them fails (no partial results) and isn't retried. Domains can have their own
  upload limit and running timeout (`maps`: 150 MiB, 1800 s). Locally with
  `docker compose --profile maps`; in the chart with `tuzkam.enabled`, scaled from zero
  by KEDA. See `docs/maps.md`.
- `GET /load` for autoscalers: `{queued, inflight, backlog_s}`, where `backlog_s` is the
  queued and in-flight pages times the measured seconds per page of their domain (an average of recent
  engine run times, `load.default_seconds_per_page` until there is one). Filter with
  `domain=a,b` and `adapter=x` (only the jobs a backend of that adapter could serve);
  `group_by=domain|adapter` shows the breakdown. It's internal: not in the OpenAPI
  schema, behind a bearer token (`LOAD_TOKEN`, Helm `secrets.loadToken`), and 404 when no
  token is configured. The counters behind it are repaired from the queues every ~10
  minutes by the cleanup worker.
- Scaling from zero: a backend keeps its last reported domains while its engine is down,
  and `taas-admin backend upsert --declared` takes capabilities (`{domain: [outputs]}`) for an
  engine that hasn't run yet; the Helm register hook declares `ocrEngine.declaredDomains`
  for every in-cluster engine. The engine's own report replaces declared ones.
- API benchmark comparing the legacy PERO protocol (driven as ProArc drives it: one page in
  flight, a new connection per page, 3 s polling) against the taas client (all pages
  submitted up front, results pushed over one WebSocket). Same corpus, same stack, so the
  delta is the client protocol alone. Run it with `make bench` (`make bench-up` for a k3d
  cold start, `DRY_RUN=1` to price the plan first); sweeps live in `bench/api/scenarios.toml`
  and results land in `bench/api/RESULTS_API.md`. Includes a toxiproxy-based latency harness
  so the WAN case is measured rather than extrapolated — every channel (API, compat,
  WebSocket, presigned MinIO downloads) pays the same RTT. See `bench/api/README.md`.
- `make check-alto-schema` — validates the ALTO TuzkaOCR emits against the real ALTO 4.4
  XSD, which nothing checked before: `build_alto()` declares 4.4 in `xsi:schemaLocation`,
  but the claim was never verified. Runs the generator over synthetic pages that hit the
  branches a normal page misses (role tags/`TAGREFS`, non-ASCII page IDs, out-of-bounds
  coordinates, empty pages, markup in `CONTENT`) plus any real output under
  `test-data/out/`; `FILES='…'` validates specific files instead. The schema and the
  xlink schema it imports are vendored under `schemas/alto/` so the check is offline and
  deterministic — see `schemas/alto/README.md` for provenance and the two substitution
  traps. Not wired into `make check`, which has to stay green without the optional
  `./TuzkaOCR` clone. Current output conforms.
- `make local-deploy-forward-bench` — port-forwards on shifted ports, so the latency proxy
  can own the published ones and presigned URLs keep working unchanged.
- Up-hours scheduling for a tunnel box (`deploy/box`): a box that has a day job — a work
  PC, a shared workstation — can now serve OCR only inside a weekly window, set with
  `OCR_UP_SCHEDULE` in its `.env` (e.g. `"Mon-Fri 17:30-07:30; Sat,Sun 00:00-24:00"`,
  plus `TZ`). Two new containers implement it, both inert at the default
  `OCR_UP_SCHEDULE=always`: a `gate` (nginx) that the tunnel now terminates at, and a
  `scheduler` that reconciles container state against the schedule. At window end the
  gate closes — `/healthz` and `POST /api/v1/process` 503, so taas drops the backend from
  rotation within `health_cache_seconds`, while status/result requests keep passing
  through, so pages already in flight finish and are harvested over the still-open tunnel.
  `OCR_DRAIN_MINUTES` (default 10) later, the engine stops and releases the GPU. Queued
  jobs just wait for the next window; only pages unfinished at the drain deadline are
  requeued. See `deploy/box/README.md`.
- Jobs now track a list of failed attempts on the `jobs.attempts` column (JSONB). Each
  entry records the stage (`dispatch`, `poll`, `engine`, `harvest`), backend, HTTP status,
  error message, retry decision, and timestamp — providing a full audit trail of every
  failure and retry for a job.
- Domains + capabilities. A job names a `domain` and the `outputs` it wants, and is routed
  only to a backend whose capabilities for that domain cover those outputs. Engines
  advertise them per domain (TuzkaOCR: an optional `capabilities` key in
  `/api/v1/models`; without it every domain is assumed to produce `alto` and `txt`), and
  domain sync stores them in `backend_domains.outputs`.
- `POST /api/v1/jobs` takes `outputs` (comma-separated, e.g. `alto,txt`) and `options` (a
  JSON object) form fields. `fmt` still works: without `outputs`, it decides them; with
  both, `outputs` wins. Options are validated at submit against a per-domain JSON Schema
  derived from the engine's own `/openapi.json` (the `POST /api/v1/process` form fields
  other than `image`, `domain` and `fmt`, closed to unknown keys), so a bad option is a
  `422` from taas instead of a failed engine job. When an engine upgrade changes that
  schema, the newest one wins and a warning is logged. An unknown domain, or outputs no
  backend for the domain can produce, are also a `422` at submit. Job status reports the
  job's `outputs`.
- `GET /api/v1/domains` lists the domains you can submit to: label, description, the
  union of outputs over the enabled backends serving each, its `options_schema`, and the
  legacy engine id the compat API uses for it.
- `pg_stat_statements` is on: the Helm chart's CloudNativePG cluster enables it
  (`cnpg.pgStatStatements`, default `true`; CNPG preloads the library and creates the
  extension), and the compose Postgres preloads it (`CREATE EXTENSION pg_stat_statements`
  to read it).

### Changed

- **Dispatch by measured speed.** Each page goes to the backend that would finish it
  first, from the backend's measured engine seconds per megapixel (the API now reads
  each image's size at submit; Pillow joins the api image) and its pages in flight,
  instead of round-robin. Backend priority tiers still come first. Failures that are a
  backend's fault make it look slower until its pages succeed again. Pages of a batch
  stick to a backend already running it when that's within `dispatch.affinity_tolerance`
  (default 0.15) of the fastest.
- **`external_id` is now an optional string** (≤ 255 characters, e.g. a PID like
  `uuid:…`), your own label, no longer a required UUID. **Breaking:** the submit field
  is `external_id` (`uuid` is gone), and WebSocket events carry `job_id` and
  `external_id` instead of `uuid`: match them on the `job_id` that submit returns. The
  engine upload is named by job id. The compat API is unaffected.
- **Retention.** Postgres keeps live state only; job history is in ClickHouse.
  Finished job rows (with their results) are deleted `jobs.done_ttl_seconds` (1 day) after
  a `done` job finished and `jobs.failed_ttl_seconds` (7 days) after a `failed` one,
  instead of after 30 days. Result objects go after `storage.results_max_age_seconds`
  (1 day), or earlier, oldest first, when `storage.results_max_bytes` is set and
  exceeded; their endpoints then answer **410 Gone**. **First deploy:** the existing
  rows past their TTL are deleted gradually, ~20 000 a minute, by the cleanup worker.
- The retry policy is now uniform across dispatch, poll, and engine-reported failures:
  transport errors, HTTP 5xx, 408, and 429 are retried at all three stages; on poll,
  HTTP 404 (engine lost the job) is also retried. Engine-reported failures with
  `error_status` ≥ 500 are retried; 4xx `error_status` (e.g. 422 bad input) fails
  immediately. The default retry budget (`jobs.max_requeues`) increased from 3 to 5.
  Each retry prefers a different backend; backoff uses full jitter (`base 1 s, cap
  30 s`). Older engines that report `error_status` only on result fetch (not on status)
  are handled by a one-shot probe: when an engine reports `failed` without `error_status`,
  the poller fetches `/api/v1/result/{id}` once to determine the HTTP status and classify
  the failure accordingly.
- Engines are now reached through the `app.engines` adapter interface (`backends.adapter`,
  default `tuzkaocr`). No behaviour change — the TuzkaOCR adapter is a 1:1 port of the
  previous `EngineClient` calls, just through a clean Protocol boundary.
- MinIO images: MinIO stopped publishing free images, and `minio/minio` and `minio/mc`
  can no longer be pulled from Docker Hub or quay.io. Compose and the Helm chart now use
  the frozen `bitnamilegacy/minio` and `bitnamilegacy/minio-client` builds (pinned, July
  2025). These get no security fixes, so treat the bundled MinIO as dev/test only and
  point production at an external S3 store. The image runs as uid 1001 and keeps its data
  under `/bitnami/minio/data`. **Existing data:** compose uses new volumes
  (`minio-*-store`; the old `minio-*-data` volumes are left untouched), and existing chart
  PVCs written by the old root-run image (same PVC, now mounted at the new path) need
  `minio.podSecurityContext.runAsUser: 0`, or a one-off `chown -R 1001`.
- The box tunnel now delivers to the `gate` service rather than straight to the engine
  (`FRP_LOCAL_IP`, default `gate`) — a pass-through unless the up-hours scheduler closes
  it. Set `FRP_LOCAL_IP=tuzkaocr` in the box `.env` to keep the old direct path.
- Verified TuzkaOCR **v1.7.0** against the box and the dispatch/harvest path: no compose,
  `frpc.toml` or app changes needed beyond the image tag. Its one client-visible change —
  an undecodable or oversized image now fails the engine job (status `failed`, 422 on
  result) instead of rejecting the `POST /api/v1/process` — is already handled: the poller
  fails such jobs on the engine's reported status, as the dispatch path did before.
- A job submitted without a `domain` is now stored and routed as `domain=default`, so it
  only reaches backends that serve `default`; before, it could go to any backend. Engines
  see the same request as before (the `default` domain is sent without a `domain` field).
- The compat API's legacy engine ids now come from the database: each domain with a
  `legacy_engine_id` is one engine (seeded `1` = `default`, `2` = `kramarky`, with the old
  labels and descriptions, so `get_engines` returns the same JSON). The shim reads them
  from `GET /api/v1/domains` with the caller's key, cached for 60 s per key. The
  `ENGINES` setting (compose, `.env.compat`, Helm `compat.engines`) is gone. A legacy
  engine is only listed while some enabled backend serves its domain.

### Removed

- **External URLs** (`users.external_url_template`, `external_url` in job status).
- **The admin API** (`/admin/*`), **the dashboard** (`/dashboard`, `/static`), the
  **master key** (`MASTER_KEY`, Helm `secrets.masterKey`, the `X-Master-Key` header) and
  the `adminDashboard` expose target. Use `taas-admin` instead; job history and
  dashboards are in ClickStack (HyperDX). `/` now redirects to `/docs`.
- **`job_analytics`** and `engine_versions` (migration 018). **Run the ClickHouse
  backfill (`python -m app.ops.backfill_clickstack`) before upgrading**: it reads the
  table this release drops. `make local-deploy-bench-db` is gone with it.

### Fixed

- The Helm chart's KEDA mode never scaled: its Redis trigger watched a list `ocr-jobs`
  that taas never writes. KEDA now scales the engine on `GET /load` (`metrics-api`,
  `keda.loadFilter`, `keda.targetBacklogSeconds`; needs `secrets.loadToken`). The
  `keda.redisAddress`, `listName`, `listLength` and `redisPassword*` values are gone.
  With `minReplicas: 0`, set `jobs.queued_timeout_seconds` above the engine's cold
  start.
- The Python client (`TaasClient`) no longer opens a fresh `httpx.AsyncClient` for every
  presigned result fetch. One pooled client now lives for the lifetime of the WebSocket
  connection, so a result download reuses the connection instead of paying a new TCP — and,
  over https, TLS — handshake per artifact. On a high-latency link that handshake cost more
  than the download itself. `AsyncTaasClient` and the Java client already reused theirs.
  Result fetches now also use the same 60 s timeout as the rest of the client, instead of
  httpx's 5 s default — that default could abort a large ALTO download over a slow link.

## [0.8.1] - 2026-07-21

### Fixed

- Jobs no longer fail with a raw `asyncpg.ConnectionDoesNotExistError` ("connection was
  closed in the middle of operation"). The engine and workers hold a connection pool
  between DB calls, and a pooled connection can be closed under them by the server (idle
  timeout, restart) or a proxy — asyncpg only notices mid-statement. The async engine now
  enables `pool_pre_ping` (validate and transparently replace a dead connection on
  checkout) and `pool_recycle` (retire connections before a server-side idle timeout can
  close them), configurable via `DB_POOL_PRE_PING` / `DB_POOL_RECYCLE_SECONDS`. This
  surfaced mainly on the compat shim's polling path, whose idle-then-reuse access pattern
  let pooled connections go stale between polls (the WebSocket path keeps them warm).
- The poller no longer marks a completed job `failed` when a transient DB or storage error
  hits during harvest. The engine has already produced the OCR output by then, so an
  infrastructure error is not the job's fault: the job is left inflight and re-harvested on
  the next poll (the reaper remains the backstop if it truly stays stuck). Genuine harvest
  failures (bad result, unexpected bug) still fail fast as before.

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
