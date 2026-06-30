#!/usr/bin/env bash
# Seed 3 test users with distinct priorities + deterministic API keys, for the local
# prioritization scenarios. Idempotent: safe to re-run (keys are set, not random).
# Requires the API port-forward (make local-deploy-forward-up).
set -euo pipefail

API="${TAAS_URL:-http://localhost:8080}"
MASTER="${MASTER_KEY:-test-master-key}"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.users.env"

# name : queue-priority : deterministic api key
USERS=("alice:10:test-key-alice" "bob:5:test-key-bob" "carol:0:test-key-carol")

curl -sf "$API/healthz" >/dev/null || { echo "API not reachable at $API — run 'make local-deploy-forward-up' first" >&2; exit 1; }

: > "$OUT"
echo "Seeding ${#USERS[@]} users via $API"
for e in "${USERS[@]}"; do
  IFS=: read -r name prio key <<<"$e"
  # create (ignore 409 if it already exists)
  curl -s -o /dev/null -X POST "$API/admin/users" \
    -H "X-Master-Key: $MASTER" -H "Content-Type: application/json" \
    -d "{\"username\":\"$name\"}" || true
  # set a known key + the user's queue priority
  curl -sf -X PUT "$API/admin/users/$name/key" \
    -H "X-Master-Key: $MASTER" -H "Content-Type: application/json" \
    -d "{\"key\":\"$key\"}" >/dev/null
  curl -sf -X PATCH "$API/admin/users/$name" \
    -H "X-Master-Key: $MASTER" -H "Content-Type: application/json" \
    -d "{\"priority\":$prio}" >/dev/null
  echo "${name}_KEY=$key" >> "$OUT"
  printf '  %-6s priority=%-2s key=%s\n' "$name" "$prio" "$key"
done
echo "Saved keys to $OUT  (sourced by scenarios.sh)"
