#!/bin/bash
set -euo pipefail

TAAS_URL="${TAAS_URL:-http://localhost:8080}"
COMPAT_URL="${COMPAT_URL:-http://localhost:8001}"
API_KEY="${API_KEY:?Set API_KEY to the test user's key}"
IMAGE="${IMAGE:?Set IMAGE to path of a test TIFF/JPG file}"

echo "=== taas Direct API Test ==="

echo "1. Submitting job..."
UUID=$(python -c "import uuid; print(uuid.uuid4())")
RESPONSE=$(curl -sf -X POST "$TAAS_URL/api/v1/jobs" \
  -H "X-API-Key: $API_KEY" \
  -F "image=@$IMAGE" \
  -F "uuid=$UUID" \
  -F "fmt=multi")
echo "$RESPONSE" | python -m json.tool
JOB_ID=$(echo "$RESPONSE" | python -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "   job_id=$JOB_ID, uuid=$UUID"

echo "2. Polling status..."
while true; do
  STATUS=$(curl -sf "$TAAS_URL/api/v1/jobs/$JOB_ID" \
    -H "X-API-Key: $API_KEY" | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "   status=$STATUS"
  if [ "$STATUS" = "done" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
  sleep 3
done

if [ "$STATUS" = "failed" ]; then
  echo "   FAILED!"
  curl -sf "$TAAS_URL/api/v1/jobs/$JOB_ID" -H "X-API-Key: $API_KEY" | python -m json.tool
  exit 1
fi

echo "3. Fetching result..."
RESULT=$(curl -sf "$TAAS_URL/api/v1/jobs/$JOB_ID/result" \
  -H "X-API-Key: $API_KEY")
echo "$RESULT" | python -m json.tool

echo "4. Downloading and decompressing ALTO..."
ALTO_URL=$(echo "$RESULT" | python -c "import sys,json; r=json.load(sys.stdin)['results']; print([x['url'] for x in r if x['fmt']=='alto'][0])")
curl -sf "$ALTO_URL" | python -c "
import sys, zstandard
data = sys.stdin.buffer.read()
print(zstandard.ZstdDecompressor().decompress(data).decode()[:500])
print('...')
"

echo "5. Downloading and decompressing TXT..."
TXT_URL=$(echo "$RESULT" | python -c "import sys,json; r=json.load(sys.stdin)['results']; print([x['url'] for x in r if x['fmt']=='txt'][0])")
curl -sf "$TXT_URL" | python -c "
import sys, zstandard
data = sys.stdin.buffer.read()
print(zstandard.ZstdDecompressor().decompress(data).decode())
"

echo ""
echo "=== taas Direct API: PASS ==="

echo ""
echo "=== Compat Legacy API Test ==="

echo "6. POST /post_processing_request..."
COMPAT_RESP=$(curl -sf -X POST "$COMPAT_URL/post_processing_request" \
  -H "api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"engine": 1, "images": {"testpage": null}}')
echo "$COMPAT_RESP" | python -m json.tool
REQUEST_ID=$(echo "$COMPAT_RESP" | python -c "import sys,json; print(json.load(sys.stdin)['request_id'])")

echo "7. GET /get_engines..."
curl -sf "$COMPAT_URL/get_engines" -H "api-key: $API_KEY" | python -m json.tool

echo "8. POST /upload_image/$REQUEST_ID/testpage..."
curl -sf -X POST "$COMPAT_URL/upload_image/$REQUEST_ID/testpage" \
  -H "api-key: $API_KEY" \
  -F "file=@$IMAGE"
echo "   upload OK"

echo "9. Polling /request_status/$REQUEST_ID..."
while true; do
  RS=$(curl -sf "$COMPAT_URL/request_status/$REQUEST_ID" \
    -H "api-key: $API_KEY")
  STATE=$(echo "$RS" | python -c "import sys,json; print(json.load(sys.stdin)['request_status']['testpage']['state'])")
  echo "   state=$STATE"
  if [ "$STATE" = "PROCESSED" ]; then
    break
  fi
  sleep 5
done

echo "10. GET /download_results/$REQUEST_ID/testpage/txt..."
curl -sf "$COMPAT_URL/download_results/$REQUEST_ID/testpage/txt" \
  -H "api-key: $API_KEY"

echo ""
echo "11. GET /download_results/$REQUEST_ID/testpage/alto..."
curl -sf "$COMPAT_URL/download_results/$REQUEST_ID/testpage/alto" \
  -H "api-key: $API_KEY" | head -20
echo "..."

echo ""
echo "=== Compat Legacy API: PASS ==="
