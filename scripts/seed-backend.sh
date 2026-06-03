#!/bin/bash
set -euo pipefail

TAAS_URL="${TAAS_URL:-http://localhost:8080}"
MASTER_KEY="${MASTER_KEY:-test-master-key}"
OCR_ENGINE_URL="${OCR_ENGINE_URL:-http://ocr-engine:8000}"
OCR_ENGINE_API_KEY="${OCR_ENGINE_API_KEY:-test-engine-key}"

echo "Waiting for taas API..."
until curl -sf "$TAAS_URL/healthz" > /dev/null 2>&1; do
  sleep 2
done

echo "Registering OCR engine backend..."
curl -sf -X POST "$TAAS_URL/admin/backends" \
  -H "X-Master-Key: $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"$OCR_ENGINE_URL\",
    \"label\": \"tuzka-cpu-1\",
    \"api_key\": \"$OCR_ENGINE_API_KEY\",
    \"max_inflight\": 4
  }" | python -m json.tool

echo "Creating test user..."
curl -sf -X POST "$TAAS_URL/admin/users" \
  -H "X-Master-Key: $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"username": "testuser"}' | python -m json.tool

echo ""
echo "Done. Save the api_key from the output above."
