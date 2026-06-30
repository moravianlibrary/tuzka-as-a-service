#!/usr/bin/env bash
# Analytics DB-scale benchmark: seed a large synthetic job_analytics fact table and
# time the real dashboard analytics queries against it. Answers "how do the analytics
# pages hold up with N million rows?" without running N million OCR jobs.
#
# Seeded rows are tagged fmt='bench' so they can be removed without touching real data.
# They are spread over the last $DAYS days (default 365) with realistic per-column
# distributions so the GROUP BY / percentile / index paths behave like production.
#
# Requires: stack up (make local-deploy-up). Talks to the DB directly via kubectl;
# no port-forward or API needed.
#
#   ./bench-analytics-db.sh                 # seed 10M rows, ANALYZE, run timed queries
#   ROWS=1000000 ./bench-analytics-db.sh    # smaller run
#   MODE=query ./bench-analytics-db.sh      # skip seeding, just time queries on current data
#   MODE=clean ./bench-analytics-db.sh      # delete seeded (fmt='bench') rows
set -euo pipefail

DB_POD="${DB_POD:-taas-db-1}"
ROWS="${ROWS:-10000000}"        # total synthetic rows to seed
BATCH="${BATCH:-1000000}"       # rows per INSERT (keeps WAL/memory bounded, shows progress)
DAYS="${DAYS:-365}"             # spread submitted_at over the last N days
WINDOW_DAYS="${WINDOW_DAYS:-30}" # query window the timed queries scan (recent N days)
MODE="${MODE:-seed}"            # seed | query | clean

psql() { kubectl exec "$DB_POD" -c postgres -- psql -U postgres -d taas -P pager=off -v ON_ERROR_STOP=1 "$@"; }
psqlc() { psql -tAc "$1"; }

count_bench() { psqlc "SELECT count(*) FROM job_analytics WHERE fmt='bench'"; }
count_all()   { psqlc "SELECT count(*) FROM job_analytics"; }

if [ "$MODE" = "clean" ]; then
  echo "Deleting seeded rows (fmt='bench')…"
  psqlc "DELETE FROM job_analytics WHERE fmt='bench'" >/dev/null
  echo "Remaining job_analytics rows: $(count_all)"
  exit 0
fi

if [ "$MODE" = "seed" ]; then
  echo "============================================================"
  echo "Seeding job_analytics: target +$ROWS rows in batches of $BATCH"
  echo "  spread over the last $DAYS days; tag fmt='bench'"
  echo "============================================================"
  existing=$(count_bench)
  [ "$existing" != "0" ] && echo "  note: $existing bench rows already present (this adds more)"

  # Add a little lookup-table variety so the breakdown GROUP BY has realistic
  # cardinality (engine versions / domains). Safe + idempotent.
  psqlc "INSERT INTO engine_versions (name) VALUES ('1.3.0'),('1.4.1'),('1.5.0'),('2.0.0')
         ON CONFLICT (name) DO NOTHING" >/dev/null
  psqlc "INSERT INTO domains (name) VALUES ('default'),('newspapers'),('manuscripts'),('maps')
         ON CONFLICT (name) DO NOTHING" >/dev/null

  # NOTE: kubectl exec drops stdin without -i, so the INSERT goes via -c (not a
  # heredoc). Batch size + day-span are plain integers we control, so they are inlined
  # into the SQL directly (psql does not interpolate :vars in -c reliably). The :: casts
  # are Postgres syntax, not bash/psql substitutions.
  build_insert() {  # build_insert <rows>
    local n="$1"
    printf '%s' "
WITH ids AS (
  SELECT (SELECT array_agg(id) FROM users)           AS uids,
         (SELECT array_agg(id) FROM engine_versions) AS evids,
         (SELECT array_agg(id) FROM backends)        AS bids,
         (SELECT array_agg(id) FROM domains)         AS dids
)
INSERT INTO job_analytics (
  job_id, external_id, submitted_at, stat_date,
  user_id, engine_version_id, engine_device, backend_id, domain_id,
  fmt, status, file_size_bytes,
  system_queue_s, engine_queue_s, ocr_running_s, time_in_system_s,
  alto_lines, alto_blocks, alto_chars, mean_conf)
SELECT
  gen_random_uuid(), gen_random_uuid(), g.ts, g.ts::date,
  ids.uids[1 + floor(random()*array_length(ids.uids,1))::int],
  ids.evids[1 + floor(random()*array_length(ids.evids,1))::int],
  (ARRAY['gpu','cpu']::engine_device_t[])[1 + floor(random()*2)::int],
  ids.bids[1 + floor(random()*array_length(ids.bids,1))::int],
  CASE WHEN random() < 0.7
       THEN ids.dids[1 + floor(random()*array_length(ids.dids,1))::int] END,
  'bench',
  (CASE WHEN random() < 0.95 THEN 'done' ELSE 'failed' END)::job_status_t,
  (50000 + random()*2000000)::bigint,
  random()*3, random()*1, random()*60, random()*70,
  (random()*400)::int, (random()*40)::int, (random()*5000)::int, random()
FROM (
  SELECT now() - (random() * ${DAYS}) * interval '1 day' AS ts
  FROM generate_series(1, ${n})
) g
CROSS JOIN ids;"
  }

  done_rows=0
  start=$(date +%s)
  while [ "$done_rows" -lt "$ROWS" ]; do
    n=$(( ROWS - done_rows ))
    [ "$n" -gt "$BATCH" ] && n=$BATCH
    psql -q -c "$(build_insert "$n")"
    done_rows=$(( done_rows + n ))
    printf '  seeded %d / %d  (%ds elapsed)\n' "$done_rows" "$ROWS" "$(( $(date +%s) - start ))"
  done

  echo "ANALYZE job_analytics…"
  psqlc "ANALYZE job_analytics" >/dev/null
fi

echo
echo "============================================================"
echo "Table size now: $(count_all) rows total ($(count_bench) bench)"
psql -c "SELECT pg_size_pretty(pg_total_relation_size('job_analytics')) AS total_size,
                pg_size_pretty(pg_indexes_size('job_analytics'))      AS index_size;"
echo "============================================================"

# Time the real dashboard analytics queries over the recent $WINDOW_DAYS-day window.
# EXPLAIN ANALYZE runs the query for real and reports planning + execution time.
timed() {  # timed <label> <sql>
  echo
  echo "── $1"
  psql -c "EXPLAIN (ANALYZE, BUFFERS, TIMING) $2" 2>&1 \
    | grep -E 'Planning Time|Execution Time' | sed 's/^/   /'
}

FROM_TS="now() - interval '$WINDOW_DAYS days'"
TO_TS="now()"

timed "breakdown (GROUP BY time/user/engine/device/domain + p95)" "
SELECT DATE_TRUNC('day', ja.submitted_at) AS time_bucket, u.username, ev.name,
       ja.engine_device, d.name,
       COUNT(*), COUNT(*) FILTER (WHERE ja.status='done'),
       COUNT(*) FILTER (WHERE ja.status='failed'),
       AVG(ja.ocr_running_s),
       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ja.ocr_running_s),
       AVG(ja.alto_lines), AVG(ja.alto_chars), AVG(ja.mean_conf)
FROM job_analytics ja
LEFT JOIN users u ON u.id = ja.user_id
LEFT JOIN engine_versions ev ON ev.id = ja.engine_version_id
LEFT JOIN domains d ON d.id = ja.domain_id
WHERE ja.submitted_at BETWEEN $FROM_TS AND $TO_TS
GROUP BY time_bucket, u.username, ev.name, ja.engine_device, d.name
ORDER BY time_bucket DESC LIMIT 50 OFFSET 0;"

timed "raw page (newest 51 rows in window)" "
SELECT ja.job_id, u.username, ev.name, d.name, ja.status, ja.ocr_running_s
FROM job_analytics ja
LEFT JOIN users u ON u.id = ja.user_id
LEFT JOIN engine_versions ev ON ev.id = ja.engine_version_id
LEFT JOIN domains d ON d.id = ja.domain_id
WHERE ja.submitted_at >= $FROM_TS
ORDER BY ja.submitted_at DESC LIMIT 51 OFFSET 0;"

timed "stats.csv yearly aggregation (per day/user/engine/domain + p50/p95/p99)" "
SELECT ja.stat_date, u.username, ev.name, d.name,
       COUNT(*), COUNT(*) FILTER (WHERE ja.status='done'),
       COUNT(*) FILTER (WHERE ja.status='failed'),
       AVG(ja.ocr_running_s), STDDEV_POP(ja.ocr_running_s),
       PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY ja.ocr_running_s),
       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ja.ocr_running_s),
       PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ja.ocr_running_s)
FROM job_analytics ja
LEFT JOIN users u ON u.id = ja.user_id
LEFT JOIN engine_versions ev ON ev.id = ja.engine_version_id
LEFT JOIN domains d ON d.id = ja.domain_id
WHERE ja.stat_date >= (now() - interval '365 days')::date
GROUP BY ja.stat_date, u.username, ev.name, d.name
ORDER BY ja.stat_date;"

timed "stats/years (DISTINCT year)" "
SELECT DISTINCT EXTRACT(YEAR FROM stat_date)::int FROM job_analytics ORDER BY 1 DESC;"

echo
echo "============================================================"
echo "WORKER HOT-PATH QUERIES (run on every job — what actually matters)"
echo "  These hit jobs (by PK / status index) + the job_analytics INSERT. Only the"
echo "  INSERT touches the big fact table; it should stay O(log n) as rows grow."
echo "============================================================"

# A write timed inside an explicit txn so EXPLAIN ANALYZE executes for real but is
# rolled back (no rows actually added). All statements share one psql -c session.
timed_write() {  # timed_write <label> <sql>
  echo
  echo "── $1"
  psql -c "BEGIN; EXPLAIN (ANALYZE, BUFFERS, TIMING) $2 ; ROLLBACK;" 2>&1 \
    | grep -E 'Planning Time|Execution Time' | sed 's/^/   /'
}

# Poller harvest: the per-job analytics INSERT (PK + 5 secondary indexes maintained).
timed_write "poller: INSERT job_analytics (1 row, ON CONFLICT)" "
INSERT INTO job_analytics (
  job_id, external_id, submitted_at, stat_date,
  user_id, engine_version_id, engine_device, backend_id, domain_id,
  fmt, status, file_size_bytes, ocr_running_s, mean_conf)
VALUES (
  gen_random_uuid(), gen_random_uuid(), now(), now()::date,
  (SELECT id FROM users LIMIT 1),
  (SELECT id FROM engine_versions LIMIT 1),
  'gpu', (SELECT id FROM backends LIMIT 1), (SELECT id FROM domains LIMIT 1),
  'txt', 'done', 123456, 3.5, 0.9)
ON CONFLICT (job_id) DO NOTHING"

# Poller write_analytics_row FK lookups (per job).
timed "poller: SELECT users by username (FK lookup)" "
SELECT id FROM users WHERE username = (SELECT username FROM users LIMIT 1);"
timed "poller: SELECT engine_versions by name (FK lookup)" "
SELECT id FROM engine_versions WHERE name = (SELECT name FROM engine_versions LIMIT 1);"

# Poller harvest: read the job row + its backend device (by PK).
timed "poller: SELECT job + backend by id (harvest read)" "
SELECT j.*, b.device FROM jobs j LEFT JOIN backends b ON b.id = j.backend_id
WHERE j.id = (SELECT id FROM jobs LIMIT 1);"

# Submit dispatch: flip a job to running (by PK).
timed_write "submit: UPDATE jobs SET status='running' WHERE id (by PK)" "
UPDATE jobs SET status = status WHERE id = (SELECT id FROM jobs LIMIT 1)"

# Reaper sweep (cleanup worker): scan for stuck jobs (uses ix_jobs_status).
timed "reaper: SELECT jobs WHERE status IN ('queued','running')" "
SELECT id, status, dispatched_at, submitted_at FROM jobs
WHERE status IN ('queued','running');"

echo
echo "Done. To remove the synthetic rows:  MODE=clean $0"
