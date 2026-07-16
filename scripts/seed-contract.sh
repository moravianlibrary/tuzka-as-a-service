#!/usr/bin/env bash
# Predefined seeding for the Schemathesis contract run.
#
# The fuzzer only ever sees 401/empty responses on the authed and collection endpoints
# unless real data exists first, so this seeds a stable API user (for X-API-Key auth), a
# backend, and one queued job. It prints `API_KEY=<key>` on the last line so the caller
# (`make test-contract`) can capture the key and pass it as a header.
#
# The seeded job doubles as a delete-guard: DELETE /admin/users/{fixture} returns 409 while
# the user owns a job, so the fuzzer can't delete the auth fixture. It can still rotate the
# key (it reuses the username from GET /admin/users responses), so `make test-contract`
# excludes the two key-mutation ops; every other operation stays in scope. The backend
# fixture is best-effort data for the collection endpoints; losing it mid-run is fine.
set -euo pipefail

TAAS_URL="${TAAS_URL:-http://localhost:8080}"
MASTER_KEY="${MASTER_KEY:-test-master-key}"
FIXTURE_USER="${FIXTURE_USER:-contract-fixture-user-do-not-delete}"

# Pick an upload image: honour $IMAGE if it exists, else the first image under test-data/.
IMAGE="${IMAGE:-}"
if [ -z "$IMAGE" ] || [ ! -f "$IMAGE" ]; then
  # -print -quit stops at the first match in find itself; piping to `head -1` would SIGPIPE
  # find (exit 141) and, under `set -o pipefail`, abort the whole script.
  IMAGE=$(find test-data -maxdepth 1 -type f \( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' \) -print -quit 2>/dev/null)
fi

PY="${PYTHON:-python3}"
api() { curl -sf -H "X-Master-Key: $MASTER_KEY" "$@"; }
extract_key() { "$PY" -c "import sys,json;print(json.load(sys.stdin).get('api_key',''))"; }

# User — create it, or (if it already exists from a prior run) rotate its key. Either path
# yields a fresh, usable key without depending on the destructive DELETE endpoint. Retry a
# few times so a not-yet-warm app (healthz can answer before the DB pool is ready) doesn't
# leave us with an empty key that would silently run the whole contract suite unauthed.
key=""
for _ in 1 2 3 4 5; do
  resp=$(api -X POST "$TAAS_URL/admin/users" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$FIXTURE_USER\"}" 2>/dev/null || true)
  [ -z "$resp" ] && resp=$(api -X POST "$TAAS_URL/admin/users/$FIXTURE_USER/rotate-key" 2>/dev/null || true)
  key=$(printf '%s' "$resp" | extract_key 2>/dev/null || true)
  [ -n "$key" ] && break
  sleep 1
done
if [ -z "$key" ]; then
  echo "seed-contract: could not obtain an API key from $TAAS_URL (is it up and MASTER_KEY correct?)" >&2
  exit 1
fi

# Backend — so the admin/dashboard backend endpoints return a row (best-effort; a duplicate
# from a prior run is fine).
api -X POST "$TAAS_URL/admin/backends" -H 'Content-Type: application/json' \
  -d '{"url":"http://ocr-engine:8000","label":"contract-fixture","max_inflight":4}' \
  >/dev/null 2>&1 || true

# One queued job (no domain → no backend needed) so the job collection endpoints exercise a
# populated response body, not just the empty case.
if [ -n "$IMAGE" ] && [ -f "$IMAGE" ]; then
  job_uuid=$("$PY" -c 'import uuid;print(uuid.uuid4())')
  curl -sf -X POST "$TAAS_URL/api/v1/jobs" -H "X-API-Key: $key" \
    -F "image=@$IMAGE" -F "uuid=$job_uuid" -F "fmt=multi" >/dev/null 2>&1 || true
fi

echo "API_KEY=$key"
