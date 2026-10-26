#!/bin/bash
# Register the compose OCR engine and create a test user (prints its API key).
set -euo pipefail

OCR_ENGINE_URL="${OCR_ENGINE_URL:-http://ocr-engine:8000}"
OCR_ENGINE_API_KEY="${OCR_ENGINE_API_KEY:-test-engine-key}"
TUZKAM_URL="${TUZKAM_URL:-http://tuzkam:8000}"
TUZKAM_API_KEY="${TUZKAM_API_KEY:-test-tuzkam-key}"
export OCR_ENGINE_API_KEY TUZKAM_API_KEY
# shellcheck source=lib/taas-admin.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/taas-admin.sh"

echo "Registering OCR engine backend..."
TAAS_ADMIN_ENV=OCR_ENGINE_API_KEY taas_admin backend upsert --url "$OCR_ENGINE_URL" \
  --label tuzka-cpu-1 --api-key-env OCR_ENGINE_API_KEY --max-inflight 4

# The map engine runs only with `docker compose --profile maps`; register it then.
if docker compose ps --status running --services 2>/dev/null | grep -qx tuzkam; then
  echo "Registering TuzkaM (maps) backend..."
  TAAS_ADMIN_ENV=TUZKAM_API_KEY taas_admin backend upsert --url "$TUZKAM_URL" \
    --label tuzkam-1 --api-key-env TUZKAM_API_KEY --max-inflight 1 --adapter tuzkam \
    --declared '{"maps": ["json", "geojson", "gcp", "alto", "txt"]}'
fi

echo "Creating test user (its API key, shown once):"
taas_admin user create testuser || taas_admin user rotate-key testuser
