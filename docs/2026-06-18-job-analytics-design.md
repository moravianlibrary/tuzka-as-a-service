# taas 0.6.x — Analytics + metrics + priority + UX

> Design document. Written 2026-06-18.

---

## Context

The original plan used two separate tables (job_daily_stats rollup + job_metrics per-job). This caused awkward seams: categories couldn't be filtered across both, rollup delayed visibility, and OCR metrics would be lost after 30 days with raw jobs.

**New design: single `job_analytics` permanent fact table.**

Written in real-time at job completion (no rollup cron). Kept forever. Supports GROUP BY any dimension. Replaces job_daily_stats as the primary analytics source.

Features in this release:
1. **job_analytics** — new fact table + dashboard rewired to it
2. **Stats breakdown** — new endpoint grouping by engine × device × user × domain
3. **Resource metrics via frpc exporter proxying** — cAdvisor + GPU exporter on box tunneled through frpc; Helm creates per-exporter ClusterIP Services
4. **Per-user external UUID link** — clickable catalog link from job detail
5. **Job queue priority** — per-user + per-backend
6. **Job filtering and export** — filter by date + OCR categories; CSV export

---

## Analytics data model

### job_analytics table (permanent fact table)

All low-cardinality dimensions use SMALLINT FKs or PostgreSQL enums. `users` gets a SMALLINT surrogate PK. `domain` and `engine_version` each get a SMALLINT lookup table (insert on first use). `backend_id` stays INTEGER (backends table already has integer PK). `external_id` is UUID (same type enforced on `jobs.external_id`).

**Timing columns** map directly to lifecycle timestamps already collected:

| Column | Formula |
|---|---|
| `system_queue_s` | `dispatched_at − submitted_at` (taas Redis queue wait) |
| `engine_queue_s` | `started_at − dispatched_at` (engine internal queue wait) |
| `ocr_running_s` | `finished_at − started_at` (actual OCR) |
| `time_in_system_s` | `stored_at − submitted_at` (full end-to-end) |

Failed jobs have NULL for timings that weren't reached (e.g. `engine_queue_s` if job failed before dispatch).

```sql
-- Surrogate integer PK on users (username remains the natural/auth key)
ALTER TABLE users ADD COLUMN id SMALLINT GENERATED ALWAYS AS IDENTITY UNIQUE;

-- Low-cardinality lookup tables (insert on first use: INSERT ... ON CONFLICT DO NOTHING)
CREATE TABLE engine_versions (
    id   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE domains (
    id   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TYPE job_status_t    AS ENUM ('done', 'failed');
CREATE TYPE engine_device_t AS ENUM ('gpu', 'cpu');
CREATE TYPE job_fmt_t       AS ENUM ('alto', 'text', 'multi');

CREATE TABLE job_analytics (
    job_id              UUID PRIMARY KEY,
    external_id         UUID,                    -- caller-supplied UUID (matches jobs.external_id)
    submitted_at        TIMESTAMPTZ NOT NULL,
    stat_date           DATE NOT NULL,           -- derived from submitted_at; for GROUP BY day
    user_id             SMALLINT REFERENCES users(id),
    engine_version_id   SMALLINT REFERENCES engine_versions(id),
    engine_device       engine_device_t,
    backend_id          INTEGER REFERENCES backends(id),
    domain_id           SMALLINT REFERENCES domains(id),
    fmt                 job_fmt_t,
    status              job_status_t NOT NULL,
    file_size_bytes     BIGINT,
    system_queue_s      FLOAT,
    engine_queue_s      FLOAT,
    ocr_running_s       FLOAT,
    time_in_system_s    FLOAT,
    alto_lines          SMALLINT,
    alto_blocks         SMALLINT,
    alto_chars          SMALLINT,
    mean_conf           FLOAT
);

CREATE INDEX ON job_analytics (stat_date);
CREATE INDEX ON job_analytics (user_id, stat_date);
CREATE INDEX ON job_analytics (engine_device, stat_date);
CREATE INDEX ON job_analytics (engine_version_id, user_id);
```

**Retention**: forever. At ~100 B/row effective (SMALLINT FKs + enums + 4 FLOAT timings):
- 1M jobs → ~100 MB
- 100M jobs → ~10 GB + ~7 GB indexes = **~17 GB**

**Written**: in `harvest()` (done jobs) and the failure branch of the poller (failed jobs). Never deleted. Separate from raw `jobs` table which is still cleaned up after 30 days.

### Replacing job_daily_stats

`job_daily_stats` is dropped in migration 007. The cleanup worker rollup step is removed. Dashboard fully rewired to job_analytics from day one — no union queries, no backward compat.

### Domain routing

`domain` is no longer free-text metadata from the caller — it is a **validated routing dimension**:

1. **TuzkaOCR engine** already exposes `GET /api/v1/models` which returns `selectable_via_domain: ["default", "kramarky", ...]`
2. **taas healthcheck** reads `selectable_via_domain` and syncs into `domains` + `backend_domains`
3. **Submit endpoint** validates caller's `domain` against registered domains — returns 422 if no backend serves it
4. **Submit worker** filters candidate backends to those serving the requested domain before picking one

```sql
CREATE TABLE backend_domains (
    backend_id  INTEGER  REFERENCES backends(id) ON DELETE CASCADE,
    domain_id   SMALLINT REFERENCES domains(id)  ON DELETE CASCADE,
    PRIMARY KEY (backend_id, domain_id)
);
```

`EngineClient` gains a `get_models(url, api_key)` call (or the existing healthcheck is extended). On each healthcheck cycle, taas calls `GET /api/v1/models`, reads `selectable_via_domain`, upserts into `domains`, and rebuilds `backend_domains` for that backend. Stale domains (no longer declared) are removed from `backend_domains` but kept in `domains` for historical analytics.

**Dashboard backends tab** gains a "Domains" column showing each backend's active domains.

### backends.device field (new)

```sql
ALTER TABLE backends ADD COLUMN device TEXT NOT NULL DEFAULT 'cpu'
    CHECK (device IN ('gpu', 'cpu'));
```

`device` drives filtering in job_analytics. The existing `backends.name` field is the free-text display label.

---

## ALTO result categories — empirically designed

Measured on 101 real book/periodical scans:
```
Lines/page:  min=0  p25=31  median=35  p75=40  p90=56  max=784
Blocks/page: min=0  p25=3   median=5   p75=11  p90=19  max=72
Chars/page:  min=0  p25=953 median=1390 p75=1759 p90=2061 max=26695
```

**Line count** (content density):

| Category | Range | Meaning |
|---|---|---|
| empty | 0 | No text detected |
| sparse | 1–15 | Title page, image-heavy page |
| normal | 16–60 | Typical book/periodical (~80% of data) |
| dense | 61–300 | Multi-column, newspaper |
| very_dense | > 300 | Exceptional (max: 784) |

**Block count** (layout complexity):

| Category | Range | Meaning |
|---|---|---|
| empty | 0 | No blocks |
| simple | 1–2 | Single text stream |
| multi | 3–10 | Multi-section (median=5) |
| complex | 11–30 | Newspaper, tables |
| fragmented | > 30 | Extreme layout (max: 72) |

**Char volume** (text density):

| Category | Range |
|---|---|
| empty | 0 |
| sparse | 1–500 |
| normal | 500–3000 (~75% of data) |
| rich | > 3000 |

All categories computed at query time via SQL CASE WHEN — no category column stored in DB.

---

## Feature 1 — Stats breakdown endpoint

`GET /dashboard/stats/breakdown?from_date=&to_date=`

Queries job_analytics (JOINed with engine_versions for name display), grouped by **(engine_version_id, engine_device, username, domain)**:

```json
{
  "rows": [
    {
      "engine_version": "1.4.0",
      "engine_device": "gpu",
      "username": "MZK AltoEditor",
      "domain": "mzk",
      "jobs_total": 12400,
      "jobs_done": 12300,
      "jobs_failed": 100,
      "proc_avg_s": 2.3,
      "proc_p95_s": 5.1,
      "avg_alto_lines": 35.2,
      "avg_alto_chars": 1380.0,
      "avg_mean_conf": 0.91
    }
  ]
}
```

Dashboard "Breakdown" tab: sortable table.

---

## Feature 2 — Per-job metrics

### 2a. File size

`file_size_bytes = len(image_bytes)` captured in `submit_job` (app/routers/jobs.py). Stored on `jobs.file_size_bytes` (new BIGINT) and in job_analytics.

### 2b. ALTO metrics (harvest time)

In `harvest()` (app/workers/poller.py):
1. Decompress ALTO bytes (already in memory).
2. Parse with `xml.etree.ElementTree` — count `<TextLine>`, `<TextBlock>`, sum `CONTENT` lengths.
3. Include in job_analytics insert.

### 2c. Confidence (requires TuzkaOCR engine change)

In `TuzkaOCR/tuzkaocr/jobs.py`:
- Add `mean_conf: float | None = None` to `Job` dataclass.
- Modify `_run()` so `process_fn` returns `(result_str, mean_conf)`.

In `TuzkaOCR/api/routes.py`:
- Add `"mean_conf": job.mean_conf` to `GET /api/v1/status/{job_id}` response.

In taas `harvest()`:
- `times["mean_conf"]` arrives automatically. Include in job_analytics insert.

---

## Feature 3 — Domain sync from existing engine endpoint

No new TuzkaOCR endpoints. No hardware metrics. Domains come from the already-existing `GET /api/v1/models` which returns `selectable_via_domain`.

During each healthcheck tick, taas makes **two calls** per backend:
1. `GET /healthz` — liveness (existing)
2. `GET /api/v1/models` — reads `selectable_via_domain`, upserts into `domains`, rebuilds `backend_domains`

`EngineClient` gains a `get_models(url, api_key) -> list[str]` helper that calls `/api/v1/models` and returns `selectable_via_domain`. Called alongside the existing healthcheck. Stale domain entries are removed from `backend_domains` for that backend but kept in `domains` for historical analytics.

---

## Feature 4 — Per-user external UUID link

`external_url_template` (nullable Text) on `users`.

Example: `https://www.digitalniknihovna.cz/mzk/uuid/{uuid}`

`{uuid}` replaced client-side by dashboard JS using `job.external_id`.

Included in `GET /api/v1/jobs/{id}` response (`JobStatus` schema).

Admin API: `PUT /admin/users/{username}` exposes the new field.

---

## Feature 5 — Job queue priority

### User priority

`priority INT NOT NULL DEFAULT 0` on `users`.

Multi-level ZSET queues: `jobs:pending:{N}` scored by `submitted_at`.

`enqueue_job()` (app/services/redis_jobs.py) ZADDs to `jobs:pending:{user.priority}`.

Submit worker (app/workers/submit.py) iterates priorities descending — drains highest first.

Old `jobs:pending` key coexists during transition; submit worker checks both names.

### Backend priority

`priority INT NOT NULL DEFAULT 0` on `backends`.

Submit worker sorts healthy backends `ORDER BY priority DESC` — fills highest-priority backend to `max_inflight` first.

---

## Feature 6 — Resource metrics via frpc exporter proxying

Remote GPU boxes run behind NAT — engines are reachable ONLY through the existing frpc reverse tunnel. One box can run N engines (e.g. 10 GPU workers) but has exactly one cAdvisor and one GPU exporter. The values schema therefore groups engines and exporters **per box**, not per engine.

### Architecture

```
cluster (Prometheus)
  └── <release>-tunnel-box-<box>-cadvisor     :8080  (ClusterIP → frps remotePort)
  └── <release>-tunnel-box-<box>-gpu-exporter :9835  (ClusterIP → frps remotePort)
  └── <release>-tunnel-engine-<box>-<engine>  :8000  (ClusterIP → frps remotePort, one per engine)
          │  single frpc tunnel (one outbound TCP per box)
          ▼
  GPU box (compose)
    ├── tuzkaocr-0 … tuzkaocr-9  :8000  (10 engines, expose only)
    ├── cadvisor                  :8080  (expose only)
    └── gpu-exporter              :9835  (expose only)
```

No taas application changes. The exporter Services are Prometheus-scrape targets; engine Services are used by taas as before.

### Helm values schema

`tunnelOcrEngines` is replaced by `tunnelBoxes`. Each box declares its engines and optional exporters:

```yaml
tunnelBoxes:
  - name: box1
    engines:
      - name: gpu0
        remotePort: 8000
      - name: gpu1
        remotePort: 8001
      # ... up to N engines
    exporters:                     # one cAdvisor/GPU exporter per box, not per engine
      - name: cadvisor
        remotePort: 8010           # unique across ALL boxes, engines, and exporters
        port: 8080                 # Service port in-cluster
      - name: gpu-exporter
        remotePort: 8011
        port: 9835
  - name: box2
    engines:
      - name: gpu0
        remotePort: 8100
    exporters:
      - name: cadvisor
        remotePort: 8110
        port: 8080
```

`tunnelBoxesDefaults` replaces `tunnelOcrEnginesDefaults` (same fields: `port`, `maxInflight`).

### Helm template changes

**`tunnel-server.yaml`** — `allowPorts` enumerates all engine and exporter remote ports across all boxes:

```toml
allowPorts = [
  { single = 8000 }, { single = 8001 },   # box1 engines
  { single = 8010 }, { single = 8011 },   # box1 exporters
  { single = 8100 },                       # box2 engine
  { single = 8110 },                       # box2 cadvisor
]
```

**`tunnel-engine.yaml`** — renamed to `tunnel-box.yaml`. Creates Services per engine and per exporter:

```
<release>-tunnel-engine-box1-gpu0          port 8000 → targetPort 8000
<release>-tunnel-engine-box1-gpu1          port 8000 → targetPort 8001
<release>-tunnel-box-box1-cadvisor         port 8080 → targetPort 8010
<release>-tunnel-box-box1-gpu-exporter     port 9835 → targetPort 8011
```

All remote ports are validated unique across all boxes, engines, and exporters at Helm render time.

### Box-side changes

**`frpc.toml`** — each engine and each exporter gets a `[[proxies]]` entry. Exporters are conditional on env vars (frp uses Go `text/template`); absent vars skip the block, so CPU boxes or boxes without exporters configured start cleanly:

```toml
# One block per engine (already supported; extended for multi-engine boxes)
[[proxies]]
name = "{{ .Envs.BOX_NAME }}-gpu0"
type = "tcp"
localIP = "tuzkaocr-0"
localPort = 8000
remotePort = {{ .Envs.REMOTE_PORT_GPU0 }}

# ... repeat per engine

{{ if .Envs.FRP_CADVISOR_REMOTE_PORT -}}
[[proxies]]
name = "{{ .Envs.BOX_NAME }}-cadvisor"
type = "tcp"
localIP = "cadvisor"
localPort = 8080
remotePort = {{ .Envs.FRP_CADVISOR_REMOTE_PORT }}
{{ end -}}

{{ if .Envs.FRP_GPU_EXPORTER_REMOTE_PORT -}}
[[proxies]]
name = "{{ .Envs.BOX_NAME }}-gpu-exporter"
type = "tcp"
localIP = "gpu-exporter"
localPort = 9835
remotePort = {{ .Envs.FRP_GPU_EXPORTER_REMOTE_PORT }}
{{ end -}}
```

The existing `frpc.toml` (single engine) and `frpc.multi.example.toml` (multi engine) are updated to reflect the new naming. A new `frpc.exporters.example.toml` shows the exporter proxy blocks.

**`compose.yaml`** — cAdvisor runs alongside all engine variants, `expose` only:

```yaml
cadvisor:
  image: ${CADVISOR_IMAGE:-gcr.io/cadvisor/cadvisor:v0.49.1}
  profiles: ["registry", "build"]
  restart: unless-stopped
  volumes:
    - /:/rootfs:ro
    - /var/run:/var/run:ro
    - /sys:/sys:ro
    - /var/lib/docker:/var/lib/docker:ro
  expose: ["8080"]
```

**`compose.gpu.yaml`** — GPU exporter in the GPU overlay (runs whenever the overlay is active):

```yaml
gpu-exporter:
  image: ${GPU_EXPORTER_IMAGE:-utkuozdemir/nvidia_gpu_exporter:1.2.1}
  runtime: nvidia
  restart: unless-stopped
  expose: ["9835"]
```

**`.env`** new optional vars:

```
FRP_CADVISOR_REMOTE_PORT=8010      # must match tunnelBoxes[].exporters[cadvisor].remotePort
FRP_GPU_EXPORTER_REMOTE_PORT=8011  # must match tunnelBoxes[].exporters[gpu-exporter].remotePort
```

### Prometheus integration

ServiceMonitor resources created per box exporter when `metrics.serviceMonitor.enabled: true` (requires Prometheus Operator). Static scrape config example for non-Operator setups:

```yaml
- job_name: taas-cadvisor
  static_configs:
    - targets:
        - '<release>-tunnel-box-box1-cadvisor:8080'
        - '<release>-tunnel-box-box2-cadvisor:8080'
- job_name: taas-gpu-exporter
  static_configs:
    - targets:
        - '<release>-tunnel-box-box1-gpu-exporter:9835'
```

---

## Feature 7 — Analytics endpoints (breakdown + raw)

### Pagination and point limits

Both endpoints use **page size 50, max 10 pages** → at most 500 rows/buckets visible per query.

### Breakdown endpoint

`GET /dashboard/analytics/breakdown`

Parameters:
- `from_date`, `to_date` — ISO-8601 datetime
- `granularity` — `hour | day | week | month | year` (default: `day`)
- `domain`, `engine_device`, `username` — optional dimension filters
- `page` — 1–10 (default: 1)

**Granularity cap**: the number of `DATE_TRUNC(granularity, submitted_at)` buckets in the requested range must be ≤ 500. Server returns HTTP 400 with a message if exceeded. Client validates before submit and shows an inline warning.

| Granularity | Max window |
|---|---|
| hour | ~20 days |
| day | ~16 months |
| week | ~9.6 years |
| month / year | unlimited |

Response groups by `(time_bucket, user_id, engine_version_id, engine_device, domain_id)` and returns `engine_version` and `domain` names via JOIN.

### Raw endpoint

`GET /dashboard/analytics/raw`

Parameters:
- `from_date`, `to_date`
- `username`, `domain`, `engine_device`, `status`
- `line_category`, `block_category`, `char_category` — mapped to BETWEEN ranges on `alto_lines` / `alto_blocks` / `alto_chars`
- `page` — 1–10 (default: 1); server returns 400 if `page > 10`

Returns up to 500 rows (50 × 10 pages). No total count — just whether there is a next page.

### Export

`GET /dashboard/analytics/raw.csv` (admin-only):
- Same filter parameters as raw endpoint (no page limit — streams full result set)
- Columns: submitted_at, job_id, external_id, username, status, fmt, domain, engine_version, engine_device, file_size_bytes, system_queue_s, engine_queue_s, ocr_running_s, time_in_system_s, alto_lines, alto_blocks, alto_chars, mean_conf
- PostgreSQL server-side cursor / StreamingResponse

---

## Migration 007

```sql
-- Drop old rollup table
DROP TABLE job_daily_stats;

-- Surrogate integer PK on users
ALTER TABLE users ADD COLUMN id SMALLINT GENERATED ALWAYS AS IDENTITY UNIQUE;

-- Low-cardinality lookup tables (populated on first use via INSERT ... ON CONFLICT DO NOTHING)
CREATE TABLE engine_versions (
    id   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE domains (
    id   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

-- Domain routing: populated from GET /api/v1/models selectable_via_domain on each healthcheck
CREATE TABLE backend_domains (
    backend_id  INTEGER  REFERENCES backends(id) ON DELETE CASCADE,
    domain_id   SMALLINT REFERENCES domains(id)  ON DELETE CASCADE,
    PRIMARY KEY (backend_id, domain_id)
);

-- Enum types
CREATE TYPE job_status_t    AS ENUM ('done', 'failed');
CREATE TYPE engine_device_t AS ENUM ('gpu', 'cpu');
CREATE TYPE job_fmt_t       AS ENUM ('alto', 'text', 'multi');

-- Analytics fact table
CREATE TABLE job_analytics (
    job_id              UUID PRIMARY KEY,
    external_id         UUID,
    submitted_at        TIMESTAMPTZ NOT NULL,
    stat_date           DATE NOT NULL,
    user_id             SMALLINT REFERENCES users(id),
    engine_version_id   SMALLINT REFERENCES engine_versions(id),
    engine_device       engine_device_t,
    backend_id          INTEGER REFERENCES backends(id),
    domain_id           SMALLINT REFERENCES domains(id),
    fmt                 job_fmt_t,
    status              job_status_t NOT NULL,
    file_size_bytes     BIGINT,
    system_queue_s      FLOAT,
    engine_queue_s      FLOAT,
    ocr_running_s       FLOAT,
    time_in_system_s    FLOAT,
    alto_lines          SMALLINT,
    alto_blocks         SMALLINT,
    alto_chars          SMALLINT,
    mean_conf           FLOAT
);
CREATE INDEX ON job_analytics (stat_date);
CREATE INDEX ON job_analytics (user_id, stat_date);
CREATE INDEX ON job_analytics (engine_device, stat_date);
CREATE INDEX ON job_analytics (engine_version_id, user_id);

-- File size on raw jobs
ALTER TABLE jobs ADD COLUMN file_size_bytes BIGINT;

-- User settings
ALTER TABLE users ADD COLUMN external_url_template TEXT;
ALTER TABLE users ADD COLUMN priority INT NOT NULL DEFAULT 0;

-- Backend priority + device type
ALTER TABLE backends ADD COLUMN priority INT NOT NULL DEFAULT 0;
ALTER TABLE backends ADD COLUMN device TEXT NOT NULL DEFAULT 'cpu'
    CHECK (device IN ('gpu', 'cpu'));
```

---

## Implementation phases for job_analytics population

**Phase 1 — basic job fields** (no engine changes needed):
`job_id`, `external_id`, `submitted_at`, `stat_date`, `user_id`, `engine_version_id`, `engine_device`, `backend_id`, `domain_id`, `fmt`, `status`, `file_size_bytes`, `system_queue_s`, `engine_queue_s`, `ocr_running_s`, `time_in_system_s`

Write in `harvest()` and the failure branch of the poller.

**Phase 2 — ALTO metrics** (harvest-time parsing):
Add `alto_lines`, `alto_blocks`, `alto_chars` via `xml.etree.ElementTree`.

**Phase 3 — confidence** (requires TuzkaOCR engine change):
Add `mean_conf` once TuzkaOCR exposes it in the status response.

---

## Files to change

| Area | Files |
|---|---|
| Migration | `alembic/versions/007_analytics_and_priority.py` |
| Submit: file size | `app/routers/jobs.py` |
| Harvest: insert job_analytics | `app/workers/poller.py` |
| Failure branch: insert job_analytics | `app/workers/poller.py` (same file, failed path) |
| Rollup removal | `app/workers/cleanup.py` (remove rollup step) |
| Engine: mean_conf | `TuzkaOCR/tuzkaocr/jobs.py`, `TuzkaOCR/tuzkaocr/pipeline.py`, `TuzkaOCR/api/routes.py` |
| Domain sync | `app/services/engine_client.py` |
| Stats breakdown endpoint | `app/routers/dashboard.py`, `app/schemas/dashboard.py` |
| Dashboard: rewire charts to job_analytics | `app/routers/dashboard.py` |
| User priority + URL template | `app/models/user.py`, `app/routers/admin.py`, `app/schemas/` |
| Backend priority + device | `app/models/backend.py`, `app/routers/admin.py`, `app/schemas/` |
| Queue: per-priority Redis keys | `app/services/redis_jobs.py`, `app/workers/submit.py` |
| Job filtering + export | `app/routers/jobs.py`, `app/routers/dashboard.py`, `app/schemas/job.py` |
| Job status: expose URL template | `app/routers/jobs.py`, `app/schemas/job.py` |
| Dashboard UI | `app/static/index.html` |
| Helm: tunnelBoxes schema | `deploy/helm/taas/values.yaml` (rename `tunnelOcrEngines` → `tunnelBoxes`) |
| Helm: frps allowPorts + Services | `deploy/helm/taas/templates/tunnel-server.yaml`, `deploy/helm/taas/templates/tunnel-engine.yaml` → `tunnel-box.yaml` |
| Box: exporter services | `deploy/box/compose.yaml`, `deploy/box/compose.gpu.yaml` |
| Box: frpc exporter proxies | `deploy/box/frpc.toml`, `deploy/box/frpc.multi.example.toml`, new `deploy/box/frpc.exporters.example.toml` |
