#!/bin/bash
set -euo pipefail

# Wait for MinIO to be ready, then create buckets
echo "Waiting for MinIO incoming..."
until mc alias set incoming http://minio-incoming:9000 "$MINIO_INCOMING_ACCESS_KEY" "$MINIO_INCOMING_SECRET_KEY" 2>/dev/null; do
  sleep 2
done
mc mb --ignore-existing incoming/incoming

echo "Waiting for MinIO results..."
until mc alias set results http://minio-results:9010 "$MINIO_RESULTS_ACCESS_KEY" "$MINIO_RESULTS_SECRET_KEY" 2>/dev/null; do
  sleep 2
done
mc mb --ignore-existing results/results

echo "MinIO buckets ready."
