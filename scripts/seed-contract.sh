#!/usr/bin/env bash
# Predefined seeding for the Schemathesis contract run.
#
# The fuzzer only ever sees 401/empty responses on the authed and collection endpoints
# unless real data exists first, so this seeds a stable API user (for X-API-Key auth), a
# backend, and one queued job. It prints `API_KEY=<key>` on the last line so the caller
# (`make test-contract`) can capture the key and pass it as a header.
#
# The seeded job makes the job collection endpoints return a populated body. Users and
# backends are managed with taas-admin (no HTTP admin API to fuzz any more).
set -euo pipefail

TAAS_URL="${TAAS_URL:-http://localhost:8080}"
# shellcheck source=lib/taas-admin.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/taas-admin.sh"
FIXTURE_USER="${FIXTURE_USER:-contract-fixture-user-do-not-delete}"

# Pick an upload image: honour $IMAGE if it exists, else the first image under test-data/.
IMAGE="${IMAGE:-}"
if [ -z "$IMAGE" ] || [ ! -f "$IMAGE" ]; then
  # -print -quit stops at the first match in find itself; piping to `head -1` would SIGPIPE
  # find (exit 141) and, under `set -o pipefail`, abort the whole script.
  IMAGE=$(find test-data -maxdepth 1 -type f \( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' \) -print -quit 2>/dev/null)
fi

PY="${PYTHON:-python3}"

# User — create it, or (if it already exists from a prior run) rotate its key; either way
# we get a fresh, usable key. Retry a few times so a not-yet-migrated DB doesn't leave us
# with an empty key that would silently run the whole contract suite unauthed.
key=""
for _ in 1 2 3 4 5; do
  key=$(taas_admin user create "$FIXTURE_USER" 2>/dev/null \
    || taas_admin user rotate-key "$FIXTURE_USER" 2>/dev/null || true)
  [ -n "$key" ] && break
  sleep 1
done
if [ -z "$key" ]; then
  echo "seed-contract: could not obtain an API key with taas-admin (is the stack up?)" >&2
  exit 1
fi

# Backend — so /api/v1/domains has something to list once domain sync runs (best-effort).
taas_admin backend upsert --url http://ocr-engine:8000 --label contract-fixture \
  --max-inflight 4 >/dev/null 2>&1 || true

# One queued job (no domain → no backend needed) so the job collection endpoints exercise a
# populated response body, not just the empty case.
if [ -n "$IMAGE" ] && [ -f "$IMAGE" ]; then
  curl -sf -X POST "$TAAS_URL/api/v1/jobs" -H "X-API-Key: $key" \
    -F "image=@$IMAGE" -F "external_id=contract-fixture" -F "fmt=multi" >/dev/null 2>&1 || true
fi

echo "API_KEY=$key"
