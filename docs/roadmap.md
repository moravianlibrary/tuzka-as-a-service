# Roadmap — 2026-10-16

Where taas goes next: the agreed direction for each theme, how they fit together, and
the build order. Each theme gets its own `plan-*.md` before any code.

Today: one job is one page, and there's one output kind (ALTO/txt).

- **Domains already exist and route jobs.** Each engine advertises its domains
  (`default`, `handwritten`, `kramarky`, `kurrent`) via `GET /api/v1/models`
  (`selectable_via_domain`). The healthcheck syncs them into `domains` and
  `backend_domains`, the submit worker routes a domain-tagged job only to a
  backend serving that domain, and analytics break down by domain.
- **The compat shim** maps legacy engine ids to domains
  (`compat/app/config.py`: 1 → default, 2 → kramarky).
- **API keys** can do everything; a user has only a `priority`.
- **Backends** have a fixed `priority` and `max_inflight`.
- **Admin is an HTTP API** (`app/routers/admin.py`, master key). The Helm register
  hook `curl`s `PUT /admin/backends`. The dashboard is a custom page over Postgres.

Each theme below gets its own `plan-*.md` before any code. **A0 + A** shape B, C and D.
**E** builds on D. **F** and **G** are independent.

## A0. Engine adapters — one interface, one implementation per engine — agreed

taas talks to engines only through **one interface**. Each engine gets its own
implementation. The workers (submit, poller, healthcheck) never know which engine
is behind it.

```python
class EngineAdapter(Protocol):
    async def describe(self) -> EngineInfo: ...  # domains, outputs + options JSON Schema per domain, version, limits
    async def submit(self, image: bytes, domain: str, outputs: set[str], options: dict) -> str: ...
    async def status(self, engine_job_id: str) -> EngineStatus: ...  # state, progress, error_status, metrics
    async def fetch(self, engine_job_id: str, output: str) -> Result: ...  # bytes + content type

ADAPTERS = {"tuzkaocr": TuzkaOcrAdapter, "tuzkam": TuzkaMAdapter}
```

- **Each backend has an adapter id** (`backends.adapter`). It's set explicitly
  (CLI / Helm register hook), never guessed from the engine's responses.
- **Domain and outputs are loose strings**, checked against what registered
  backends advertise. They're not enums in code, so a new engine needs no taas
  release.
- **Request shape:** `domain` + `outputs` + `options`. A field is top-level only
  if **taas itself acts on it** (routing, scopes, storage, scaling, analytics).
  Everything only the engine reads, e.g. `alto_profile`, `source_identifier`,
  `physical_img_nr`, TuzkaM's `region` and `georef`, goes in `options`. There
  is one flat level of options; no "common" vs "adapter" tiers.
- **Options are derived from the engine's own OpenAPI,** not hand-written in
  taas:
  - The adapter reads the engine's `/openapi.json` and takes the request body
    schema of `POST /api/v1/process`, minus the fields taas owns (`image`, `fmt`,
    `domain`, …). What's left is the options JSON Schema for that domain, returned
    by `describe()`.
  - It's remembered with the capabilities, so it survives the engine being scaled
    to zero.
  - The API validates each job's `options` against it when the job is submitted.
    A field the engine doesn't declare, or a bad value, is a 4xx and never reaches
    an engine.
  - The validated options are stored on the job and passed to `submit()` as they
    are.
  - **One options schema per domain, newest wins.** When a backend's derived
    schema differs from the stored one, it replaces it and a warning is logged;
    the backend is still linked. This keeps engine rolling upgrades working.
    During a rollout, a job using a new option may hit an old pod and get a 4xx.
  - This replaces `options_schema() -> type[BaseModel]` in the current
    `app/engines/base.py`.
- **Engines must declare their options precisely.** Today both accept
  `alto_profile` as a plain string and check `ndk`/`basic` in code
  (`TuzkaOCR/api/routes.py:105`, TuzkaM `api/routes.py` `process_image`).
  Derived validation would accept any string. Change these to `Literal[...]`
  (`alto_profile`) and `bool` (`georef`), so the OpenAPI carries enums and types
  (see External dependencies).
- **Documentation:**
  - **`GET /domains`** lists each domain with its outputs and its options JSON
    Schema, copied from its engine. It's always current, and it's how clients
    discover what they can ask for.
  - taas's own `/openapi.json` is generated from code at startup, so it documents
    `options` as an object and points to `GET /domains`.
  - **Optional later:** override FastAPI's `openapi()` to embed the current
    per-domain schemas, so they show in `/docs`. That needs refreshing whenever
    backends change. Only do it if clients want everything in `/docs`.
- **First step is a pure refactor:** the old `app/services/engine_client.py`
  becomes the `tuzkaocr` adapter, with no behaviour change. This is in progress
  (`app/engines/`, commits `aeed460`…`29c505e`).
- **Outputs are an open list** (`alto`, `txt`, `json`, `geojson`, `gcp`, …), each
  stored as `{job}.{ext}.zst`.
- **A job without a domain goes only to backends serving `default`.** Today it
  can land on any backend (`app/workers/submit.py:99`), which would send text jobs
  to TuzkaM.

## A. Taxonomy — domains + capabilities — agreed

**Decision: keep the current engine domains as the document-type axis.** The names
already say what an engine is good for (kramarky, handwritten, kurrent), routing
already works on them, and so do the analytics. Don't add a parallel "profile"
concept.

- **Add more domains as needed**, as fine-grained as the models are: e.g.
  `maps-old`, `maps-new`, periodicals, manuscripts. Each one is advertised by
  whichever engine has a model for it.
- **Add a second axis: capabilities** — what an engine can *output*: `text`,
  `alto`, `coords`. Engines advertise them next to `selectable_via_domain` (e.g.
  `capabilities` in `/api/v1/models`, per domain). They're synced into
  `backend_domains` (or a sibling table) by the same healthcheck.
- **Capabilities say what *can* be produced; the job says what *is* produced.** A
  job names a domain plus the outputs it wants, which must be a subset of the
  domain's capabilities. Example: a `maps-old` engine can output
  `{alto, text, coords}`, and a job asks for just `{coords}`. The submit worker
  routes only to backends whose capabilities cover the requested outputs.
- Asking for an output the domain can't produce is a 4xx at submit time, not a
  failed job later.
- This generalizes today's `fmt=alto|txt|multi`.
- **Needs a TuzkaOCR change:** advertise capabilities per domain. Until then,
  assume `{text, alto}` for every domain.
- **Compat engine ids are derived from the domain table**, not hard-coded in
  `compat/app/config.py`.
- Open: do domains need human labels or descriptions for clients (the compat
  `get_engines` has them, the core API doesn't)?

## B. Scoped API keys — agreed

- Each key gets the domains and capabilities it may use. Possibly also per-domain
  limits (rate limit, priority).
- **Existing keys are migrated to "everything"**, so current clients keep
  working.
- Managed from the admin CLI (F).

## C. Maps — via TuzkaM — agreed

The map engine exists: **TuzkaM** (`gitlab.mzk.cz/osdd/tuzkam`). It's CPU map OCR:
it finds label regions on map sheets, reads them, and optionally matches them
against GeoNames for approximate coordinates.

- Its API is close to TuzkaOCR's: same endpoints, `X-API-Key`, 503 when full,
  `mean_conf`, and `error_status` 422/500 in the status.
- What's different:
  - It computes all outputs at once; the result endpoint picks one with
    `?fmt=json|alto|geojson|gcp|txt`.
  - Its `/api/v1/models` advertises no domains.
  - It reports `n_words` instead of `n_lines`, plus a `georef` summary,
    `progress` and `eta_s`.
  - Its job options are `georef`, `region` and `alto_profile`.
  - All of this is handled by the **`tuzkam` adapter** (A0).
- **No own coordinates format.** Expose TuzkaM's outputs as they are: `json` (has
  `schema_version`), `geojson`, `gcp`, `alto`, `txt`. The client (ProArc) edits
  MODS from them.
- Its domain(s) (e.g. `maps`) come from adapter config until TuzkaM advertises
  them itself.
- **Limits per domain:** maps need more than text pages. TuzkaM accepts up to 150
  MiB and 400 MP, but taas's upload limit is 100 MB (`app/config.py:45`) and its
  running timeout 300 s. These become per-domain config values set through the
  CLI.
- Analytics events carry its metrics: words, georef status, inliers, RMS.
- Deployed in the Helm chart as its own workload, registered with
  `adapter=tuzkam`, scaling to zero via `/load?adapter=tuzkam` (E).

## D. Batch jobs — agreed

- `POST /batches`: upload an archive and get back a batch UUID. Each page is still
  its own job inside the batch.
- **One batch = one domain + one set of outputs.** No mixed batches: that keeps
  scopes, affinity and stats simple.
  - **Formats:** `tar` + `zstd` (`.tar.zst`) first. Also accept plain `.tar`,
    `.tar.gz`, maybe `.zip`, detected by magic bytes, not by extension.
  - Unpack server-side while streaming to MinIO, so the whole upload never sits in
    memory.
- `GET /batches/{id}`: batch stats (total / done / failed / ETA). With `?pages=1`
  it also lists per-page status.
- **Results download:** a presigned MinIO URL to one `.tar.zst` of all outputs,
  as single results work today. Partial results are allowed, with a manifest.
- WebSocket push per batch, as the per-page client already gets.
- This replaces the TODO item "batch submitting and pulling".
- Open:
  - What are the upload size and page-count limits?
  - Is the results archive built once the batch finishes, or on demand?
- A batch and its results archive follow the same TTL rules as jobs (F): done
  TTL, or the longer failed TTL if any page failed.
- New API only. The compat (ProArc) API stays per page.

## E. Load balancing on measured performance and batch affinity — agreed

This replaces the static `priority × max_inflight` dispatch and the TODO formula
`prio * free_capacity`.

- **Performance score:** each finished item updates the backend's moving average
  (EWMA) of seconds per page, per domain, ideally normalized per megapixel so
  large and small pages compare fairly. Dispatch goes to the backend with the
  lowest *expected finish time*: its queue × average time, plus this page's time.
  Static `priority` remains as an operator override.
- **Batch affinity:** when scores are close, prefer the backend that already has
  items from the same batch. On a GPU this keeps the model warm and the batch
  together. Keep the preference soft, so one busy batch can't starve other users.
- This mostly makes the adaptive submit/pull timers unnecessary. Measured on k3d
  (step 8 T6): with a free slot, submit→dispatch p95 is 0.07 s — **timers not
  needed**.
- **The score is computed in the dispatch path, not read from ClickStack.**
  Workers update it as each attempt finishes (Redis hash per backend × domain).
  ClickStack is only for looking back.
- **Retries override affinity:** a retry always goes to a *different* backend.
  Affinity only breaks ties between backends with close scores.
- The performance score can come first; affinity needs D.

**Autoscaling — `GET /load`:**

- `GET /load` returns **one total** for the jobs matching its filters: `queued`,
  `inflight`, and `backlog_s` (queued × average seconds per page). Backlog seconds
  is the scaling signal, because a map takes minutes and a text page seconds.
  - `domain=a,b,…`: jobs in any of these domains.
  - `adapter=x`: jobs this adapter can serve, i.e. whose domain *and* requested
    outputs are in its capabilities.
  - The filters can be combined. No filter returns the total.
  - The sum over domains is exact: each job has exactly one domain, so nothing is
    counted twice.
- **No pool label.** A workload is scaled by *what it can serve*, not by which
  workload it is. Adding a GPU or a CPU replica makes no difference to the queue.
- **One KEDA ScaledObject per engine workload**, using the `metrics-api` scaler:

  ```yaml
  triggers:
    - type: metrics-api
      metadata:
        url: "http://taas-api:8000/load?adapter=tuzkam"
        valueLocation: "backlog_s"
        targetValue: "600"           # one replica per 10 min of queued work
        activationTargetValue: "0"   # wake from zero once anything is queued
  ```

  Typical filters:

  | Workload | Filter |
  |---|---|
  | general TuzkaOCR | `?adapter=tuzkaocr` |
  | specialised workload (e.g. handwritten models only) | `?domain=handwritten,kurrent` |
  | TuzkaM | `?adapter=tuzkam` |

- **Overlap is a deployment choice:** if two scaled workloads cover the same
  domain, both see its backlog and both scale. Handle it with thresholds, or by
  scaling only one of them.
- **Optional overview for humans:** `group_by=domain|adapter` returns the same
  numbers per group. Autoscaling doesn't use it.
- **Scaling from zero needs capabilities while no pod is up:**
  - Keep each backend's **last-known capabilities** instead of clearing them when
    the pod disappears. Today the healthcheck rebuilds `backend_domains` from
    whatever the engine returns.
  - **For a first start that has never been seen,** the registration (Helm values
    / CLI) declares the expected domains and outputs. They're replaced by the
    engine's real answer once a pod is up.
- The numbers come from Redis counters per domain, updated on enqueue, dispatch
  and finish. Not from ClickStack.
- Read-only and internal: on the cluster Service, not the public ingress.
- **Scale to zero** (e.g. TuzkaM, `minReplicaCount: 0`). This needs the queued
  timeout to be longer than the cold start, and the dispatcher to keep jobs
  queued while no backend serves the domain.
- **Fixes an existing bug:** the chart's KEDA mode watches a Redis list
  `ocr-jobs` (`deploy/helm/taas/values.yaml:184`) that never exists. Jobs are
  queued in `jobs:pending:{priority}` sorted sets. Prod uses `hpa` (CPU), so it's
  unaffected. The `/load` trigger replaces it.

## F. Ops — ClickStack for analytics, admin CLI instead of an admin API — agreed

**Decision:**

- Use an **existing dashboard solution, not our own.** Job analytics move out of
  Postgres into **ClickStack** (ClickHouse + HyperDX).
- **Failure details stay in Postgres:** error messages and the per-job attempt
  list (G). ClickStack gets only an error flag (plus maybe an error class) per
  event, so failure rates can be charted without copying the details there.
- **Postgres keeps only live state:** users, keys, backends, config, and `jobs`
  only while they're active.
  - **Done jobs** are deleted when a TTL after finishing runs out (hours to a
    day). Downloading doesn't end a job: a result can be downloaded any number
    of times until then.
  - **Failed jobs** get a longer TTL, so their `attempts` list (G) can still be
    inspected through the CLI.
  - History lives in ClickStack only. (Today jobs are kept 30 days:
    `RETENTION_DAYS`, `app/workers/cleanup.py:28`.)
- All configuration goes through a **CLI that talks to the DB directly**. The
  admin HTTP API, the custom dashboard and its admin forms are removed.
- **No backward compatibility for the deployment:** remove them in one go; no
  transition period.

**Measured on prod (2026-10-15, read-only, from the API pod):**

| Table | Rows | Size | Note |
|---|---|---|---|
| `jobs` | 2.37M | 840 MB | since 2026-09-09 — about 65k/day, October ~100k/day |
| `job_analytics` | 4.15M | 1.2 GB | since 2026-07-02, kept forever |
| `job_results` | 2.27M | 1.2 GB | |

| Dashboard query | Time | Why |
|---|---|---|
| analytics breakdown, 30 days, by day | **26 s** | sorts ~2.3M rows with `percentile_cont`; spills ~150 MB to disk |
| `count(*)` on analytics, 90 days | 2.7 s | sequential scan |
| backends view: `DISTINCT backend_id` on jobs | **6 s** | full index scan, on every page load |
| usage chart, 30 days | 1.7 s | sequential scan — `jobs` has no index on `submitted_at` alone |
| stats cards, 24 h (×4 queries) | 0.6–0.8 s each | same missing index |
| jobs list, newest 50 | 0.9 s | same |

- **Postgres is undersized:** `shared_buffers` = 128 MB, `work_mem` = 4 MB, the DB
  pod uses ~290 MiB, and `pg_stat_statements` isn't installed.
- **Conclusion:** every slow query is a query over *history*: the analytics
  table, and `jobs` as a 30-day archive. In the new design, history lives in
  ClickHouse and `jobs` holds only active work (thousands of rows, not millions),
  so these queries go away rather than getting fixed. No index or tuning work on
  the current tables.
- Worth keeping: enable `pg_stat_statements`, so slow queries are visible from
  now on.

**Analytics:**

- Workers emit one event per job state change (submitted, dispatched, finished,
  failed + error flag, with timings, backend, domain, user, page size, ALTO
  stats) to ClickHouse. This could go via OpenTelemetry, so HyperDX gets logs and
  traces too.
- **ClickStack runs self-hosted in `dk-taas`**, deployed by our Helm chart next to
  the rest of taas.
- **Dashboards are code:** a versioned `dashboard.json` in the repo is loaded
  automatically on first start (e.g. a Helm hook / init job that imports it), so
  a fresh deployment comes with working dashboards and nobody clicks them
  together by hand. It's re-imported whenever the file changes.
- HyperDX can load dashboards this way. It's already done in another Helm chart
  of ours, so reuse that setup rather than designing it again.
- Backfill the existing `job_analytics` rows once, then drop the table and its
  queries.
- Live operational numbers (queue depth, inflight per backend) are emitted as
  metrics to the same stack.

**Admin CLI:**

- Python (e.g. Typer): users and keys + scopes (B), backends, domains and
  capabilities (A), config, plus read-only views of failed jobs and their
  attempts (G).
- Shipped as an admin image running in the cluster. Operators use it via
  `kubectl exec`, so k8s RBAC is the access control, with a secret key as an
  optional extra.
- Validation (today's pydantic schemas in `app/schemas/`) moves into shared code
  the CLI uses.
- The Helm register hook (`backend-register-job.yaml`) runs the CLI image instead
  of `curl PUT /admin/backends`.

**Open:**

- What are the actual TTL values, for done jobs and for failed jobs? They should
  be config values set through the CLI.
- `job_results` (1.2 GB today) and the result objects in MinIO follow the same
  TTL as their job.

## G. Softer failures — retry more, fail less — agreed

Retries already exist, but the budget is small and several failure paths skip it
entirely.

**Today:**

- **Dispatch:** transport errors and 5xx are requeued to another backend, up to
  `jobs.max_requeues` (default **3**), and the failing backend is sidelined
  (`is_transient_dispatch_error`, `app/workers/submit.py:49`). Every other 4xx
  fails at once — including 408 and 429, which are transient.
- **Poll:** a transport error counts as `unreachable` and is requeued within the
  same budget. **Any other poll error fails the job immediately**
  (`check_one`, `app/workers/poller.py:146`). That includes an engine 5xx, and a
  404 because the engine restarted and lost the job.
- **Engine-reported `failed`/`error` fails at once** (`classify_poll_result`).
  That's right for a corrupt image, but not for an OOM, a CUDA error or a killed
  worker, which would likely pass on another backend.
- **Harvest:** transient errors already retry until the reaper gives up. That's
  fine as is.

**Plan:**

- **One retry policy, one budget per job,** applied to dispatch, poll and
  engine-reported failure alike:
  - **Retry:** transport errors, 5xx, 408, 429, a poll 404 (job lost), and
    engine-reported failures with `error_status` 5xx (see below).
  - **Don't retry:** other 4xx (bad image, auth, unsupported domain), and
    engine-reported failures with `error_status` 422 (bad input).
  - Each retry goes to a *different* backend where possible, with exponential
    backoff and jitter.
- **Bigger default budget** (e.g. 5), still set through `jobs.max_requeues`. A
  separate total-time limit stops a job from bouncing for hours.
- **No new flag is needed. TuzkaOCR already classifies its failures.** An
  undecodable image is `JobInputError` → `error_status=422`; anything else
  (OOM, CUDA, crash) → `500` (`TuzkaOCR/tuzkaocr/jobs.py:140`). Bad input is
  mostly rejected at submit time anyway (400/413).
  - **Gap:** the status endpoint returns only the `error` text, not
    `error_status`. The code only appears when the result is fetched
    (`TuzkaOCR/api/routes.py:343`).
  - **Engine change (one line):** add `error_status` to the status response.
  - **Stopgap until then:** on `failed`, fetch the result endpoint once and read
    its HTTP status code.
- **Feeds E:** every failed attempt lowers the backend's performance score, so a
  flaky backend gets less work without anyone disabling it by hand.
- **Attempts are a list on the job** (e.g. a JSONB `attempts` column: backend,
  stage, HTTP status / error, timestamp). It's cleaned up with the job. ClickStack
  gets only the attempt event with an error flag.
- **Batches (D):** a failed page never fails the batch. It shows up as failed in
  the batch stats and the manifest. Maybe also add `POST /batches/{id}/retry` to
  resubmit only the failed pages.
- Open:
  - Should a client be able to set its own retry budget per job or per batch?
  - Should a job that failed on *every* backend be marked as "the input is bad"
    rather than retried more?

## How it fits together

- **A0 + A are the backbone.** One adapter interface hides each engine's
  protocol; domains + capabilities decide routing. Scopes (B) are
  written in those terms. Maps (C) are just a domain with a `coords` capability. A
  batch (D) is pages + one domain + one set of outputs.
- **G feeds E:** failed attempts lower the backend's score. **D feeds E:** batches
  give affinity something to group by. Retries override affinity.
- **Live numbers are computed where they're used:** E's score lives in Redis,
  updated in the dispatch path. ClickStack is for looking back only.
- **Postgres = live state only (active jobs + their failures, config).
  ClickStack = all history, analytics + metrics. CLI = all configuration.** No
  admin API, no custom dashboard.
- **Compat shim:** its engine ids come from the domain table (A). It stays per
  page, with no batches.

## Suggested order

**Everything below ships together as `0.9.0`.** The order is about clean
dependencies, not about what lands in which version. It's a 0.x release, so the
breaking changes (admin API and custom dashboard removed) need no major version;
the CHANGELOG calls them out.

| # | Step | Verify with |
|---|---|---|
| 1 | **G** retries (`error_status`, one retry policy, `attempts` list) | unit tests for the classifier; kill an engine mid-job on k3d |
| 2 | **A0** adapter interface — `tuzkaocr` as a pure refactor | `make check`, `make test-integration`, `bench/api` unchanged |
| 3 | **A** domains + capabilities, `GET /domains`, options derived from engine OpenAPI; `pg_stat_statements` on | integration test per domain; ALTO schema check under both `alto_profile`s |
| 4 | **`/load`** + remembered/declared capabilities + KEDA `metrics-api` (replaces the broken `ocr-jobs` trigger) | scale up and to zero on k3d under `bench/api` load |
| 5 | **F** ClickStack: deploy, emit job events, `dashboard.json`, backfill `job_analytics` | dashboard numbers match a Postgres count for one day |
| 6 | **F** admin CLI + **B** scopes — admin API, custom dashboard and `job_analytics` removed, short job TTLs on | CLI covers everything the admin API did; Helm hook uses it |
| 7 | **D** batches | `bench/api`: batch vs per-page |
| 8 | **E** performance score + batch affinity | `bench/api` before/after; mixed GPU/CPU backends |
| 9 | **C** TuzkaM adapter, per-domain limits, TuzkaM workload — can move right after step 4 if maps are urgent | JSON/GeoJSON on sample map sheets; scale from zero |

- **`/load` is its own step (4):** C's scale-to-zero depends on it, so it lands
  before C wherever C ends up.
- **Step 5 must land before step 6:** history has to be in ClickStack before
  anything deletes it from Postgres.

**External dependencies (TuzkaOCR, TuzkaM):**

- TuzkaOCR: capabilities per domain (A), and `error_status` in the status
  response (G, one line). Both have fallbacks.
- Both engines: declare `process` options with precise types (`alto_profile:
  Literal["ndk", "basic"]`, `georef: bool`), so options derived from their
  OpenAPI validate properly (A0). Until then, taas accepts any string for these
  fields and the engine rejects bad values.
- TuzkaM (C) otherwise works as it is.
