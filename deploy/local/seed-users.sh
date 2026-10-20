#!/usr/bin/env bash
# Seed 3 test users with distinct priorities + deterministic API keys, for the local
# prioritization scenarios. Idempotent: safe to re-run (keys are set, not random).
# Requires the API port-forward (make local-deploy-forward-up).
set -euo pipefail

API="${TAAS_URL:-http://localhost:8080}"
# shellcheck source=../../scripts/lib/taas-admin.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../scripts/lib/taas-admin.sh"
export TAAS_ADMIN_MODE="${TAAS_ADMIN_MODE:-k3d}"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.users.env"

# name : queue-priority : deterministic api key
USERS=("alice:10:test-key-alice" "bob:5:test-key-bob" "carol:0:test-key-carol")

curl -sf "$API/healthz" >/dev/null || { echo "API not reachable at $API — run 'make local-deploy-forward-up' first" >&2; exit 1; }

: > "$OUT"
echo "Seeding ${#USERS[@]} users with taas-admin"
for e in "${USERS[@]}"; do
  IFS=: read -r name prio key <<<"$e"
  # create (refused if it already exists), then set a known key + the queue priority
  taas_admin user create "$name" >/dev/null 2>&1 || true
  USER_KEY="$key" TAAS_ADMIN_ENV=USER_KEY taas_admin user set-key "$name" --key-env USER_KEY >/dev/null
  taas_admin user set-priority "$name" "$prio" >/dev/null
  echo "${name}_KEY=$key" >> "$OUT"
  printf '  %-6s priority=%-2s key=%s\n' "$name" "$prio" "$key"
done
echo "Saved keys to $OUT  (sourced by scenarios.sh)"
