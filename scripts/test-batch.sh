#!/usr/bin/env bash
# End-to-end batch test against a running compose stack (make up first).
#
# Packs N copies of a test image into a .tar.zst, submits it as one batch, polls
# GET /batches/{id} until it finishes, downloads the results archive, and checks that
# the manifest lists every page as done with its outputs.
#
# Env overrides:
#   IMAGE          page image                (default: test-data/sample.jpg)
#   PAGES          copies of it in the batch (default: 5)
#   TAAS_URL       host API base             (default: http://localhost:8080)
#   BATCH_TIMEOUT  seconds to wait           (default: 900)
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-test-data/sample.jpg}"
PAGES="${PAGES:-5}"
TAAS_URL="${TAAS_URL:-http://localhost:8080}"
BATCH_TIMEOUT="${BATCH_TIMEOUT:-900}"
PY="${PY:-python3}"
# shellcheck source=lib/taas-admin.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/taas-admin.sh"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
jget() { "$PY" -c 'import sys,json; d=json.load(sys.stdin); print(d'"$1"')'; }

[ -f "$IMAGE" ] || die "test image not found: $IMAGE"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say "Packing $PAGES pages into a .tar.zst"
mkdir -p "$WORK/pages"
ext="${IMAGE##*.}"
for i in $(seq 1 "$PAGES"); do cp "$IMAGE" "$WORK/pages/page-$i.$ext"; done
echo "not an image" > "$WORK/pages/README.txt"  # must be skipped, not fail the batch
tar --zstd -cf "$WORK/batch.tar.zst" -C "$WORK" pages
ok "$(du -h "$WORK/batch.tar.zst" | cut -f1) archive"

say "Creating a fresh user"
API_KEY="$(taas_admin user create "batch-test-$(date +%s)")"
[ -n "$API_KEY" ] || die "could not create a user"

say "Submitting the batch"
resp="$(curl -sf -X POST "$TAAS_URL/api/v1/batches" -H "X-API-Key: $API_KEY" \
  -F "archive=@$WORK/batch.tar.zst" -F "external_id=test-batch" -F "outputs=alto,txt")"
BATCH_ID="$(echo "$resp" | jget '["batch_id"]')"
[ "$(echo "$resp" | jget '["total"]')" = "$PAGES" ] || die "expected $PAGES pages: $resp"
[ "$(echo "$resp" | jget '["skipped"]')" = "['pages/README.txt']" ] || die "skipped: $resp"
ok "batch=$BATCH_ID"

say "Waiting for it to finish"
deadline=$(( $(date +%s) + BATCH_TIMEOUT ))
while :; do
  status="$(curl -sf "$TAAS_URL/api/v1/batches/$BATCH_ID" -H "X-API-Key: $API_KEY")"
  [ "$(echo "$status" | jget '["status"]')" = finished ] && break
  [ "$(date +%s)" -lt "$deadline" ] || die "not finished after ${BATCH_TIMEOUT}s: $status"
  sleep 3
done
[ "$(echo "$status" | jget '["done"]')" = "$PAGES" ] || die "not every page done: $status"
ok "all $PAGES pages done"

say "Downloading the results archive"
url="$(curl -sf "$TAAS_URL/api/v1/batches/$BATCH_ID/results" -H "X-API-Key: $API_KEY" \
  | jget '["url"]')"
curl -sf -o "$WORK/results.tar.zst" "$url" || die "download failed"
tar --zstd -tf "$WORK/results.tar.zst" > "$WORK/listing"
grep -qx manifest.json "$WORK/listing" || die "no manifest.json in the archive"
tar --zstd -xf "$WORK/results.tar.zst" -C "$WORK" manifest.json
"$PY" - "$WORK/manifest.json" "$PAGES" <<'PYEOF'
import json, sys
manifest = json.load(open(sys.argv[1]))
pages = int(sys.argv[2])
assert len(manifest) == pages, f"{len(manifest)} entries, expected {pages}"
for entry in manifest:
    assert entry["status"] == "done", entry
    assert set(entry["outputs"]) == {"alto", "txt"}, entry
PYEOF
ok "manifest lists $PAGES done pages with alto + txt"
echo "PASS"
