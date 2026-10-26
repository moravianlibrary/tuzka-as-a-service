# ClickStack files

- `schema.sql` — the ClickHouse schema (`taas.job_events`, `taas.live_metrics`). The one
  copy: the chart's init Job applies it on every install/upgrade, docker-compose mounts
  it into ClickHouse's initdb directory.
- `dashboard.json` — the HyperDX dashboards, imported by the chart's
  `clickstack-dashboards` Job (`python -m app.ops.import_dashboard`) on every
  install/upgrade: created by name, or updated in place.

**`dashboard.json` is a hand-written placeholder.** It follows the HyperDX export format
(see k7-kramerius' `Access Logs Dashboard.json`) but has never been loaded into HyperDX.
Replace it with a real export: build the dashboards once in a running HyperDX (pages done
per domain, total time p50/p95 per domain, failures by error class, run time per
backend, queue depth), export them, and commit the export here.

The tiles name their **source**, and the importer maps names to ids. Create these
sources in HyperDX before the first import (the Job fails, naming the missing ones):

| Source | Table | Timestamp column |
|---|---|---|
| `Job Events` | `taas.job_events` | `ts` |
| `Live Metrics` | `taas.live_metrics` | `ts` |
