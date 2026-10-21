#!/usr/bin/env bash
# End-to-end smoke test for the taas stack, on docker compose or the local k3d deploy.
#
# compose: brings up the full stack (unless NO_UP=1) and registers the TuzkaOCR
# backend. k3d (TARGET=k3d): uses the running deploy as it is (the chart registered the
# backends; port-forwards from `make local-deploy-forward-up`). Either way it creates a
# fresh user, submits an image, polls to completion, then fetches and decompresses
# the result. Result URLs are presigned with the *public* MinIO endpoint, so — like a
# real download client — we fetch them from the host and decompress them there (zstd).
#
# Env overrides:
#   TARGET               compose | k3d           (default: compose)
#   IMAGE                path to test image      (default: test-data/sample.jpg)
#   TAAS_URL             host API base           (default: http://localhost:8080)
#   OCR_ENGINE_URL       compose engine URL      (default: http://ocr-engine:8000)
#   OCR_ENGINE_API_KEY   engine api key          (default: test-engine-key)
#   JOB_TIMEOUT          seconds to wait for OCR (default: 900)
#   NO_UP=1              compose: skip `compose up`/build (use already-running stack)
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="${TARGET:-compose}"
IMAGE="${IMAGE:-test-data/sample.jpg}"
TAAS_URL="${TAAS_URL:-http://localhost:8080}"
# shellcheck source=lib/taas-admin.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/taas-admin.sh"
OCR_ENGINE_URL="${OCR_ENGINE_URL:-http://ocr-engine:8000}"
OCR_ENGINE_API_KEY="${OCR_ENGINE_API_KEY:-test-engine-key}"
JOB_TIMEOUT="${JOB_TIMEOUT:-900}"
FMT="${FMT:-multi}"
RESULTS_DIR="${RESULTS_DIR:-test-data/out}"   # where decompressed results are saved
SHOW_RESULT="${SHOW_RESULT:-1}"               # 1 = print a preview of each result
PREVIEW_LINES="${PREVIEW_LINES:-25}"

PY="${PY:-python3}"
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$IMAGE" ] || die "test image not found: $IMAGE (set IMAGE=...)"
command -v "$PY" >/dev/null || die "python3 not found (set PY=...)"
command -v zstd >/dev/null || die "zstd not found (results are zstd-compressed)"
case "$TARGET" in compose|k3d) ;; *) die "TARGET must be compose or k3d" ;; esac

# jq-free JSON field extraction via python
jget() { "$PY" -c 'import sys,json; d=json.load(sys.stdin); print(d'"$1"')'; }

# ---------------------------------------------------------------------------
if [ "$TARGET" = compose ] && [ "${NO_UP:-0}" != "1" ]; then
  say "Building and starting the stack (this is slow on first run — TuzkaOCR model load)"
  docker compose up -d --build
fi

say "Waiting for API at $TAAS_URL/healthz"
for i in $(seq 1 60); do
  curl -sf "$TAAS_URL/healthz" >/dev/null 2>&1 && break
  [ "$i" = 60 ] && die "API never became healthy"
  sleep 2
done
ok "API healthy"

if [ "$TARGET" = compose ]; then
  say "Waiting for ocr-engine container to report healthy"
  cid="$(docker compose ps -q ocr-engine)"
  [ -n "$cid" ] || die "ocr-engine container not found"
  for i in $(seq 1 120); do
    h="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo none)"
    [ "$h" = healthy ] && break
    [ "$i" = 120 ] && die "ocr-engine never became healthy (status=$h)"
    sleep 5
  done
  ok "ocr-engine healthy"

  # ---------------------------------------------------------------------------
  say "Registering OCR backend (idempotent)"
  TAAS_ADMIN_ENV=OCR_ENGINE_API_KEY taas_admin backend upsert --url "$OCR_ENGINE_URL" \
    --label tuzka-cpu --api-key-env OCR_ENGINE_API_KEY --max-inflight 4 >/dev/null \
    || die "backend upsert failed"
  ok "backend registered"
else
  say "Backends registered by the chart:"
  taas_admin backend list
fi

say "Creating a fresh test user"
USERNAME="taas-test-$(date +%s)"
API_KEY="$(taas_admin user create "$USERNAME")"
[ -n "$API_KEY" ] || die "failed to obtain api_key"
ok "user=$USERNAME"

# ---------------------------------------------------------------------------
EXT_ID="test-$(basename "$IMAGE")"
say "Submitting job (image=$IMAGE external_id=$EXT_ID fmt=$FMT)"
JOB_ID="$(curl -sf -X POST "$TAAS_URL/api/v1/jobs" \
  -H "X-API-Key: $API_KEY" \
  -F "image=@$IMAGE" -F "external_id=$EXT_ID" -F "fmt=$FMT" | jget '["job_id"]')"
[ -n "$JOB_ID" ] || die "submit failed"
ok "job_id=$JOB_ID"

say "Polling status (timeout ${JOB_TIMEOUT}s)"
deadline=$(( $(date +%s) + JOB_TIMEOUT ))
while :; do
  STATUS="$(curl -sf "$TAAS_URL/api/v1/jobs/$JOB_ID" -H "X-API-Key: $API_KEY" | jget '["status"]')"
  printf '    status=%s\n' "$STATUS"
  case "$STATUS" in
    done) break ;;
    failed)
      err="$(curl -sf "$TAAS_URL/api/v1/jobs/$JOB_ID" -H "X-API-Key: $API_KEY" | jget '.get("error")')"
      die "job failed: $err" ;;
  esac
  [ "$(date +%s)" -ge "$deadline" ] && die "timed out waiting for job"
  sleep 5
done
ok "job done"

# ---------------------------------------------------------------------------
say "Fetching result URLs"
RESULT_JSON="$(curl -sf "$TAAS_URL/api/v1/jobs/$JOB_ID/result" -H "X-API-Key: $API_KEY")"
echo "$RESULT_JSON" | "$PY" -m json.tool
n="$(echo "$RESULT_JSON" | jget '["results"].__len__()')"
[ "$n" -ge 1 ] || die "no result entries returned"

say "Fetching + saving results"
mkdir -p "$RESULTS_DIR"
base="$(basename "$IMAGE")"; base="${base%.*}"
echo "$RESULT_JSON" | "$PY" -c 'import sys,json
for r in json.load(sys.stdin)["results"]:
    print(r["fmt"], r["url"])' | while read -r fmt url; do
  ext="xml"; [ "$fmt" = txt ] && ext="txt"
  out="$RESULTS_DIR/${base}.${fmt}.${ext}"
  # The presigned URL targets the public endpoint, reachable from the host.
  curl -sf "$url" -o "$out.raw" || die "result '$fmt' download failed"
  if [ "$(head -c 4 "$out.raw" | od -An -tx1 | tr -d ' \n')" = 28b52ffd ]; then
    zstd -dcq "$out.raw" > "$out"   # zstd frame magic
  else
    cat "$out.raw" > "$out"
  fi
  rm -f "$out.raw"
  size="$(wc -c < "$out")"
  [ "$size" -gt 0 ] || die "result '$fmt' empty or unreachable"
  ok "result fmt=$fmt -> ${size} bytes -> $out"

  if [ "$SHOW_RESULT" = 1 ]; then
    printf '\033[2m----- %s (%s) first %s lines -----\033[0m\n' "$fmt" "$ext" "$PREVIEW_LINES"
    head -n "$PREVIEW_LINES" "$out"
    printf '\033[2m----- end -----\033[0m\n'
    if [ "$fmt" = alto ]; then
      printf '\033[2m----- OCR text extracted from ALTO (first %s lines) -----\033[0m\n' "$PREVIEW_LINES"
      "$PY" - "$out" "$PREVIEW_LINES" <<'PYEOF'
import sys, xml.etree.ElementTree as ET
path, limit = sys.argv[1], int(sys.argv[2])
root = ET.parse(path).getroot()
nsuri = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
tl = f"{{{nsuri}}}TextLine" if nsuri else "TextLine"
st = f"{{{nsuri}}}String" if nsuri else "String"
n = 0
for line in root.iter(tl):
    txt = " ".join(s.get("CONTENT", "") for s in line.iter(st) if s.get("CONTENT"))
    if txt.strip():
        print(txt)
        n += 1
        if n >= limit:
            break
PYEOF
      printf '\033[2m----- end -----\033[0m\n'
    fi
  fi
done

printf '\n\033[1;32mPASS\033[0m  full pipeline ok: submit -> dispatch -> OCR -> harvest -> result\n'
