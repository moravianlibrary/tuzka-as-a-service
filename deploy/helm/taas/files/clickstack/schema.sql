-- ClickHouse schema for taas analytics (step 5). The one copy: docker-compose mounts it
-- into the clickhouse initdb directory, and the Helm chart's init Job applies it on every
-- install/upgrade. Every statement is idempotent.

CREATE DATABASE IF NOT EXISTS taas;

-- One row per job state change (app/services/events.py). No error text: that stays in
-- Postgres with the job; this has the flag and a coarse class to chart.
CREATE TABLE IF NOT EXISTS taas.job_events
(
    ts              DateTime64(3),
    event           LowCardinality(String),   -- submitted | dispatched | retried | done | failed
    job_id          UUID,
    batch_id        Nullable(UUID),
    username        LowCardinality(String),
    domain          LowCardinality(String),
    outputs         Array(LowCardinality(String)),
    backend_id      Nullable(UInt32),
    adapter         LowCardinality(String),
    engine_version  LowCardinality(String),
    device          LowCardinality(String),
    error_flag      UInt8,
    error_class     LowCardinality(String),   -- http_4xx | http_5xx | transport | engine_input
                                              -- | engine_internal | timeout | ''
    http_status     Nullable(UInt16),
    file_size_bytes Nullable(UInt64),
    queue_s         Nullable(Float32),        -- submitted -> dispatched (taas clock)
    engine_queue_s  Nullable(Float32),        -- engine received -> started (engine clock)
    run_s           Nullable(Float32),        -- started -> finished (engine clock)
    store_s         Nullable(Float32),        -- finished -> stored
    total_s         Nullable(Float32),        -- submitted -> stored (or failed)
    lines           Nullable(UInt32),
    blocks          Nullable(UInt32),
    chars           Nullable(UInt32),
    mean_conf       Nullable(Float32),
    extra           Map(String, String)       -- adapter-specific metrics
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (event, ts);

-- Live numbers sampled every 15 s (queue depth, in-flight per backend), from the /load
-- counters (step 4).
CREATE TABLE IF NOT EXISTS taas.live_metrics
(
    ts     DateTime64(3),
    metric LowCardinality(String),
    labels Map(String, String),
    value  Float64
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (metric, ts);
