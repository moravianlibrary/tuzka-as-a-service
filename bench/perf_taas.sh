#!/usr/bin/env bash
# Whole-taas measurement (plan 10 T4): drive a running compose stack whose backends are
# fake engines (bench/fake_engine), and collect what PERF_TAAS.md reports.
#
#   1. reset pg_stat_statements and snapshot Redis INFO commandstats
#   2. Locust against the API for DURATION (submit + status + result mix)
#   3. read back: Locust's CSV (latency percentiles, throughput), the top SQL
#      statements, Redis commands per submitted job, and the job events' queue_s /
#      total_s percentiles from ClickHouse when CLICKHOUSE_URL is set
#
# Profiles of the api and workers aren't automated: run, while the load is on,
#   py-spy record -d 60 -o OUT/<proc>.svg --pid "$(docker inspect -f '{{.State.Pid}}' <container>)"
# (py-spy on the host, as root).
#
# Env: TAAS_URL (http://localhost:8080), USERS (50), RATE (10), DURATION (2m),
#      OUT (bench/perf_out/<timestamp>), CLICKHOUSE_URL (optional)
set -euo pipefail
cd "$(dirname "$0")/.."

TAAS_URL="${TAAS_URL:-http://localhost:8080}"
USERS="${USERS:-50}"
RATE="${RATE:-10}"
DURATION="${DURATION:-2m}"
OUT="${OUT:-bench/perf_out/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"

psql_taas() { docker compose exec -T postgres psql -U taas -d taas -At -c "$1"; }
commandstats() {
  docker compose exec -T redis redis-cli INFO commandstats | tr -d '\r' \
    | sed -n 's/^cmdstat_\([a-z|]*\):calls=\([0-9]*\).*/\1 \2/p' | sort
}

echo "==> resetting pg_stat_statements, snapshotting Redis commandstats"
psql_taas "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" >/dev/null
psql_taas "SELECT pg_stat_statements_reset()" >/dev/null
commandstats > "$OUT/redis_before.txt"
jobs_before="$(psql_taas "SELECT count(*) FROM jobs")"

echo "==> Locust: $USERS users, $RATE/s spawn, $DURATION"
key="$(TAAS_URL="$TAAS_URL" bash scripts/seed-contract.sh | sed -n 's/^API_KEY=//p')"
TAAS_API_KEY="$key" uv run --extra dev locust -f tests/load/locustfile.py --host "$TAAS_URL" \
  --headless -u "$USERS" -r "$RATE" -t "$DURATION" --csv "$OUT/locust" >"$OUT/locust.log" 2>&1

echo "==> collecting"
commandstats > "$OUT/redis_after.txt"
jobs_after="$(psql_taas "SELECT count(*) FROM jobs")"
submitted=$(( jobs_after - jobs_before ))
python3 - "$OUT" "$submitted" <<'PY'
import sys
out, jobs = sys.argv[1], max(int(sys.argv[2]), 1)
def load(name):
    return {c: int(n) for c, n in (l.split() for l in open(f"{out}/{name}") if l.strip())}
before, after = load("redis_before.txt"), load("redis_after.txt")
rows = sorted(((after[c] - before.get(c, 0)) / jobs, c) for c in after)
with open(f"{out}/redis_per_job.txt", "w") as f:
    for per_job, cmd in reversed(rows):
        if per_job > 0:
            f.write(f"{per_job:8.2f}  {cmd}\n")
print(f"{jobs} jobs submitted")
PY
psql_taas "SELECT round(total_exec_time::numeric, 1) AS total_ms, calls,
             round(mean_exec_time::numeric, 3) AS mean_ms, left(query, 160)
           FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20" \
  > "$OUT/pg_top_statements.txt"
if [ -n "${CLICKHOUSE_URL:-}" ]; then
  curl -sf "$CLICKHOUSE_URL" --data-binary "
    SELECT event, count(), quantile(0.5)(queue_s), quantile(0.95)(queue_s),
           quantile(0.5)(total_s), quantile(0.95)(total_s)
    FROM taas.job_events WHERE ts > now() - INTERVAL 1 HOUR GROUP BY event FORMAT PrettyCompact" \
    > "$OUT/clickhouse_latency.txt" || echo "  (ClickHouse query failed)"
fi
echo "==> done: $OUT (copy the numbers into bench/PERF_TAAS.md)"
