#!/usr/bin/env bash
# Prioritization test scenarios against the local two-engine stack (gpu1 priority 10,
# cpu1 priority 0) and the seeded users (alice=10, bob=5, carol=0).
#
# Requires: stack up (make local-deploy-up), forwards (make local-deploy-forward-up),
# users seeded (make local-deploy-seed). Observations are read from the DB via kubectl.
#
#   N=6 ./scenarios.sh           # 6 jobs per burst (override with N=...)
set -euo pipefail

API="${TAAS_URL:-http://localhost:8080}"
DB_POD="${DB_POD:-taas-db-1}"
N="${N:-6}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mapfile -t IMAGES < <(ls "$HERE"/../../test-data/*_image.jpeg 2>/dev/null)

# shellcheck disable=SC1091
source "$HERE/.users.env" 2>/dev/null || { echo "run 'make local-deploy-seed' first" >&2; exit 1; }
[ "${#IMAGES[@]}" -gt 0 ] || { echo "no test images in test-data/" >&2; exit 1; }

psql() { kubectl exec "$DB_POD" -c postgres -- psql -U postgres -d taas -P pager=off -c "$1"; }

submit() {  # submit <api-key> <count>  — fire jobs concurrently
  local key="$1" count="$2" i img
  for i in $(seq 1 "$count"); do
    img="${IMAGES[$((RANDOM % ${#IMAGES[@]}))]}"
    curl -sf -o /dev/null -X POST "$API/api/v1/jobs" -H "X-API-Key: $key" \
      -F "image=@$img" -F "uuid=$(uuidgen)" -F "fmt=txt" &
  done
  wait
}

wait_idle() {  # wait until no queued/running jobs (or timeout)
  local left
  for _ in $(seq 1 120); do
    left=$(kubectl exec "$DB_POD" -c postgres -- psql -U postgres -d taas -tAc \
      "SELECT count(*) FROM jobs WHERE status NOT IN ('done','failed')")
    [ "${left//[[:space:]]/}" = "0" ] && return 0
    sleep 2
  done
  echo "  (timeout waiting for jobs to drain — are both engines up + healthy?)" >&2
}

reset_jobs() { kubectl exec "$DB_POD" -c postgres -- psql -U postgres -d taas -tAc "DELETE FROM jobs" >/dev/null 2>&1 || true; }

echo "============================================================"
echo "Scenario A — BACKEND prioritization (gpu1=10 preferred, spill to cpu1=0)"
echo "  Submitting $N jobs as alice; expect gpu1 to take jobs until max_inflight,"
echo "  then spill to cpu1."
echo "============================================================"
reset_jobs
submit "$alice_KEY" "$N"
wait_idle
psql "SELECT b.label AS backend, b.device, b.priority, count(*) AS jobs
      FROM jobs j JOIN backends b ON j.backend_id = b.id
      GROUP BY b.label, b.device, b.priority
      ORDER BY b.priority DESC;"

echo
echo "============================================================"
echo "Scenario B — USER prioritization (alice=10 jumps ahead of carol=0)"
echo "  Queue a carol (low) burst, then an alice (high) burst; the priority queue"
echo "  drains jobs:pending:10 before :0, so alice's jobs start first."
echo "============================================================"
reset_jobs
submit "$carol_KEY" "$N"
submit "$alice_KEY" "$N"
wait_idle
psql "SELECT j.username, u.priority AS user_prio,
             to_char(min(j.started_at),'HH24:MI:SS.MS') AS first_start,
             to_char(max(j.started_at),'HH24:MI:SS.MS') AS last_start
      FROM jobs j JOIN users u ON j.username = u.username
      GROUP BY j.username, u.priority
      ORDER BY u.priority DESC;"
echo "  (alice's started_at window should precede carol's despite carol submitting first.)"

echo
echo "Done. Inspect live in the dashboard (Backends + Analytics tabs):"
echo "  http://localhost:8080/   master key: test-master-key"
