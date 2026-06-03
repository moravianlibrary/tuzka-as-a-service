#!/usr/bin/env bash
# End-to-end test for the legacy-compat server (PERO-style API).
#
# Exercises: get_engines -> post_processing_request -> upload_image ->
# request_status -> download_results, which round-trips through taas + TuzkaOCR.
# compat downloads the presigned result inside the network and returns the
# decompressed body directly, so the client just gets XML/txt back.
#
# Assumes the main stack is already up (run `make test` or `make up` first).
# Rebuilds + restarts the compat container to pick up local changes.
#
# Env overrides:
#   IMAGE   test image          (default: test-data/_e2e.jpg)
#   TAAS_URL host taas API       (default: http://localhost:8080)
#   COMPAT_URL host compat API   (default: http://localhost:8001)
#   MASTER_KEY admin master key  (default: test-master-key)
#   OCR_ENGINE_URL / OCR_ENGINE_API_KEY  (for idempotent backend registration)
#   JOB_TIMEOUT seconds          (default: 900)
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-test-data/sample.jpg}"
TAAS_URL="${TAAS_URL:-http://localhost:8080}"
COMPAT_URL="${COMPAT_URL:-http://localhost:8001}"
MASTER_KEY="${MASTER_KEY:-test-master-key}"
OCR_ENGINE_URL="${OCR_ENGINE_URL:-http://ocr-engine:8000}"
OCR_ENGINE_API_KEY="${OCR_ENGINE_API_KEY:-test-engine-key}"
JOB_TIMEOUT="${JOB_TIMEOUT:-900}"
FILENAME="page1.jpg"

PY="${PY:-python3}"
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
jget() { "$PY" -c 'import sys,json; d=json.load(sys.stdin); print(d'"$1"')'; }

[ -f "$IMAGE" ] || die "test image not found: $IMAGE (set IMAGE=...)"

say "Rebuilding + restarting compat container"
docker compose up -d --build compat >/dev/null
for i in $(seq 1 30); do
  curl -sf "$TAAS_URL/healthz" >/dev/null 2>&1 && break
  [ "$i" = 30 ] && die "taas API not healthy"
  sleep 2
done

say "Waiting for compat to be ready"
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$COMPAT_URL/get_engines" -H "api-key: x" || true)"
  [ "$code" = 200 ] && break
  [ "$i" = 30 ] && die "compat not responding (last code=$code)"
  sleep 1
done

say "Ensuring OCR backend is registered"
existing="$(curl -sf "$TAAS_URL/admin/backends" -H "X-Master-Key: $MASTER_KEY" \
  | "$PY" -c 'import sys,json; print(any(b["url"]=="'"$OCR_ENGINE_URL"'" for b in json.load(sys.stdin)))')"
if [ "$existing" != "True" ]; then
  curl -sf -X POST "$TAAS_URL/admin/backends" \
    -H "X-Master-Key: $MASTER_KEY" -H "Content-Type: application/json" \
    -d "{\"url\":\"$OCR_ENGINE_URL\",\"label\":\"tuzka-cpu\",\"api_key\":\"$OCR_ENGINE_API_KEY\",\"max_inflight\":4}" >/dev/null
fi
ok "backend ready"

say "Creating a fresh taas user (its key is the legacy api-key)"
USERNAME="compat-test-$(date +%s)"
API_KEY="$(curl -sf -X POST "$TAAS_URL/admin/users" \
  -H "X-Master-Key: $MASTER_KEY" -H "Content-Type: application/json" \
  -d "{\"username\":\"$USERNAME\"}" | jget '["api_key"]')"
[ -n "$API_KEY" ] || die "could not create user"
ok "user=$USERNAME"

# --- legacy API flow (note: compat reads the lowercase 'api-key' header) ---
say "GET /get_engines"
curl -sf "$COMPAT_URL/get_engines" -H "api-key: $API_KEY" | "$PY" -m json.tool

say "POST /post_processing_request (engine=1)"
REQ_ID="$(curl -sf -X POST "$COMPAT_URL/post_processing_request" \
  -H "api-key: $API_KEY" -H "Content-Type: application/json" \
  -d "{\"engine\":1,\"images\":{\"$FILENAME\":{}}}" | jget '["request_id"]')"
[ -n "$REQ_ID" ] || die "no request_id"
ok "request_id=$REQ_ID"

say "POST /upload_image/$REQ_ID/$FILENAME"
curl -sf -X POST "$COMPAT_URL/upload_image/$REQ_ID/$FILENAME" \
  -H "api-key: $API_KEY" -F "file=@$IMAGE" | "$PY" -m json.tool

say "Polling /request_status (timeout ${JOB_TIMEOUT}s)"
deadline=$(( $(date +%s) + JOB_TIMEOUT ))
while :; do
  STATE="$(curl -sf "$COMPAT_URL/request_status/$REQ_ID" -H "api-key: $API_KEY" \
    | jget '["request_status"]["'"$FILENAME"'"]["state"]')"
  printf '    %s=%s\n' "$FILENAME" "$STATE"
  [ "$STATE" = PROCESSED ] && break
  [ "$(date +%s)" -ge "$deadline" ] && die "timed out (last state=$STATE)"
  sleep 5
done
ok "request processed"

say "GET /download_results/$REQ_ID/$FILENAME/alto"
OUT="$(mktemp --suffix=.xml)"
code="$(curl -s -o "$OUT" -w '%{http_code}' \
  "$COMPAT_URL/download_results/$REQ_ID/$FILENAME/alto" -H "api-key: $API_KEY")"
[ "$code" = 200 ] || die "download_results returned HTTP $code: $(cat "$OUT")"
bytes="$(wc -c < "$OUT")"
head -c 200 "$OUT"; echo
grep -q '<' "$OUT" || die "downloaded result does not look like XML"
[ "$bytes" -gt 0 ] || die "empty result"
ok "downloaded ALTO XML -> ${bytes} bytes"
rm -f "$OUT"

printf '\n\033[1;32mPASS\033[0m  compat pipeline ok: legacy API -> taas -> OCR -> download\n'
