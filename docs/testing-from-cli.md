# Testing taas & taas-compat from the command line

Two copy-pasteable **bash** pipelines that exercise the full OCR round-trip against a
locally running stack (`make up`): submit an image, poll until done, download and
decompress the result.

- **taas** — the modern API on `:8080`, header `X-API-Key`. Returns presigned URLs you
  fetch + `zstd -d` yourself.
- **taas-compat** — the legacy PERO shim on `:8001`, header `api-key`. Returns the
  decompressed bytes directly.

## Prerequisites

```bash
# tools used below
command -v curl jq zstd uuidgen >/dev/null || echo "install: curl jq zstd uuidgen"

# the stack must be running
make up        # or: docker compose up -d
```

Interactive API docs are also available at <http://localhost:8080/docs> (taas) and
<http://localhost:8001/docs> (compat).

---

## taas (modern API, `:8080`)

```bash
#!/usr/bin/env bash
set -euo pipefail

TAAS=http://localhost:8080
MASTER=test-master-key                 # from .env.app
IMG=test.jpg                           # any image; or your own path
USERNAME=clitest-$RANDOM               # fresh each run; a re-used name returns 409

# 1) Create a user and capture its API key (shown only once).
KEY=$(curl -sS -X POST "$TAAS/admin/users" \
    -H "X-Master-Key: $MASTER" -H "Content-Type: application/json" \
    -d "{\"username\":\"$USERNAME\"}" | jq -r .api_key)
echo "key=$KEY"

# 2) Queue the image. uuid must be a real UUID; fmt = alto | txt | multi.
JOB=$(curl -sS -X POST "$TAAS/api/v1/jobs" \
    -H "X-API-Key: $KEY" \
    -F "image=@$IMG" \
    -F "uuid=$(uuidgen)" \
    -F "fmt=multi" | jq -r .job_id)
echo "job=$JOB"

# 3) Poll until the job leaves the queue: queued -> running -> done | failed.
while :; do
    STATUS=$(curl -sS "$TAAS/api/v1/jobs/$JOB" -H "X-API-Key: $KEY" | jq -r .status)
    echo "status=$STATUS"
    [[ "$STATUS" == "done" || "$STATUS" == "failed" ]] && break
    sleep 2
done
[[ "$STATUS" == "done" ]] || { echo "job failed"; exit 1; }

# 4) Get the result URLs (presigned, public endpoint), pick the ALTO one.
URL=$(curl -sS "$TAAS/api/v1/jobs/$JOB/result" -H "X-API-Key: $KEY" \
    | jq -r '.results[] | select(.fmt=="alto") | .url')

# 5) Download the presigned URL as-is and decompress (results are zstd).
curl -sS "$URL" -o result.alto.xml.zst
zstd -d -f result.alto.xml.zst
head result.alto.xml
```

---

## taas-compat (legacy PERO shim, `:8001`)

The compat `api-key` **is** a taas user key, so the pipeline below creates one first.
Flow: open a request → upload each image → poll → download (compat decompresses for you).

```bash
#!/usr/bin/env bash
set -euo pipefail

TAAS=http://localhost:8080
COMPAT=http://localhost:8001
MASTER=test-master-key                 # from .env.app
IMG=test.jpg                           # any image; or your own path
FNAME=test.jpg                         # logical filename, used as the key throughout
USERNAME=compat-clitest-$RANDOM        # fresh each run; a re-used name returns 409

# 1) Create a taas user; its API key doubles as the legacy api-key.
KEY=$(curl -sS -X POST "$TAAS/admin/users" \
    -H "X-Master-Key: $MASTER" -H "Content-Type: application/json" \
    -d "{\"username\":\"$USERNAME\"}" | jq -r .api_key)
echo "key=$KEY"

# 2) (optional) list available engines.
curl -sS "$COMPAT/get_engines" -H "api-key: $KEY" | jq

# 3) Open a request, declaring which filenames you'll send -> request_id.
REQ=$(curl -sS -X POST "$COMPAT/post_processing_request" \
    -H "api-key: $KEY" -H "Content-Type: application/json" \
    -d "{\"engine\": 1, \"images\": {\"$FNAME\": {}}}" | jq -r .request_id)
echo "request=$REQ"

# 4) Upload the image (this creates + queues the taas OCR job).
curl -sS -X POST "$COMPAT/upload_image/$REQ/$FNAME" \
    -H "api-key: $KEY" \
    -F "file=@$IMG" | jq

# 5) Poll per-image status: WAITING -> PROCESSING -> PROCESSED.
while :; do
    STATE=$(curl -sS "$COMPAT/request_status/$REQ" -H "api-key: $KEY" \
        | jq -r ".request_status[\"$FNAME\"].state")
    echo "state=$STATE"
    [[ "$STATE" == "PROCESSED" ]] && break
    sleep 2
done

# 6) Download once PROCESSED. format = alto | txt. compat returns decompressed
#    XML / text directly (no zstd step).
curl -sS "$COMPAT/download_results/$REQ/$FNAME/alto" -H "api-key: $KEY" -o result.xml
curl -sS "$COMPAT/download_results/$REQ/$FNAME/txt"  -H "api-key: $KEY" -o result.txt
head result.xml
```

---

## taas vs taas-compat

| | taas | taas-compat |
|---|---|---|
| Port | `8080` | `8001` |
| Auth header | `X-API-Key` | `api-key` |
| Submit | one `POST /api/v1/jobs` | `post_processing_request` → `upload_image/...` |
| Status values | `queued` / `running` / `done` / `failed` | `WAITING` / `PROCESSING` / `PROCESSED` |
| Result | presigned URLs; you `zstd -d` | decompressed XML / text directly |
