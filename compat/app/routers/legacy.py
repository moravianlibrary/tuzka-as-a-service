import asyncio
import hashlib
import time
from uuid import uuid4

import zstandard
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from starlette.responses import Response

from ..retry import request_with_retry

router = APIRouter()


def get_api_key(request: Request) -> str:
    key = request.headers.get("api-key")
    if not key:
        raise HTTPException(401, "Missing api-key header")
    return key


_valid_keys: dict[str, float] = {}
VALIDATE_CACHE_TTL_SECONDS = 30.0


async def validate_api_key(request: Request, api_key: str) -> None:
    """Verify the api-key is a valid taas user key by calling a protected endpoint.

    Successful validations are cached briefly so legacy polling loops don't
    burn the user's query rate limit on every compat call.
    """
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    now = time.monotonic()
    expires = _valid_keys.get(key_hash)
    if expires and now < expires:
        return
    http = request.app.state.http
    resp = await request_with_retry(
        http,
        "GET",
        "/api/v1/jobs",
        headers={"X-API-Key": api_key},
        params={"limit": 1},
    )
    if resp.status_code == 401:
        raise HTTPException(401, "Invalid api-key")
    _valid_keys[key_hash] = now + VALIDATE_CACHE_TTL_SECONDS


@router.post(
    "/post_processing_request",
    summary="Open a legacy OCR processing request",
    responses={
        400: {"description": "Unknown engine requested."},
        401: {"description": "Missing or invalid api-key header."},
        503: {"description": "taas rate limit could not be absorbed within the retry budget."},
    },
)
async def post_processing_request(request: Request):
    """Open a PERO-style processing request and reserve slots for its images.

    Validates the ``api-key`` header against the modern taas API (via a cached
    ``GET /api/v1/jobs`` probe), checks the requested engine, then stores the
    image filenames in compat state under a freshly minted ``request_id``. No
    taas job is created yet; images are uploaded later via ``upload_image``.
    """
    api_key = get_api_key(request)
    await validate_api_key(request, api_key)
    body = await request.json()
    engine = body.get("engine", 1)
    images = body.get("images", {})

    settings = request.app.state.settings
    if engine not in settings.engines:
        raise HTTPException(400, f"Unknown engine: {engine}")

    request_id = str(uuid4())
    state = request.app.state.compat_state
    await state.create_request(request_id, engine, list(images.keys()))

    return {"status": "success", "request_id": request_id}


@router.get(
    "/get_status",
    summary="Check that a legacy request exists",
    responses={
        400: {"description": "Unknown request_id."},
        401: {"description": "Missing or invalid api-key header."},
        503: {"description": "taas rate limit could not be absorbed within the retry budget."},
    },
)
async def get_status(request: Request, request_id: str):
    """Report whether a previously opened request_id is still known to the shim.

    Validates the ``api-key`` header against taas, then looks the request up in
    compat state (which expires after ``compat_ttl_seconds``). This is a
    liveness check only and does not query per-image OCR job progress.
    """
    api_key = get_api_key(request)
    await validate_api_key(request, api_key)
    state = request.app.state.compat_state
    req = await state.get_request(request_id)
    if not req:
        raise HTTPException(400, "Unknown request_id")
    return {"status": "success", "request_id": request_id}


@router.post(
    "/upload_image/{request_id}/{filename}",
    summary="Upload one image and create its taas OCR job",
    responses={
        400: {"description": "Filename not part of the request, or taas rejected the upload."},
        401: {"description": "Missing api-key header."},
        404: {"description": "Request not found in compat state."},
        503: {"description": "taas rate limit could not be absorbed within the retry budget."},
        "4XX": {"description": "Error forwarded verbatim from the taas job-creation call."},
        "5XX": {"description": "Error forwarded verbatim from the taas job-creation call."},
    },
)
async def upload_image(
    request: Request,
    request_id: str,
    filename: str,
    file: UploadFile = File(...),
):
    """Upload a single image for a request and start its OCR job on taas.

    Reads the upload, then forwards it as a ``POST /api/v1/jobs`` multipart call
    (carrying the ``X-API-Key`` header and the engine's optional ``domain``),
    and records the returned ``job_id`` against the filename. Unlike the other
    endpoints this trusts the caller's earlier validation and does not re-probe
    the api-key; any non-2xx taas response is surfaced with its original status
    and body.
    """
    api_key = get_api_key(request)
    state = request.app.state.compat_state
    settings = request.app.state.settings

    req = await state.get_request(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if filename not in req["filenames"]:
        raise HTTPException(400, f"Filename {filename} not in request")

    engine_cfg = settings.engines[req["engine"]]
    file_bytes = await file.read()
    external_id = str(uuid4())

    # Forward to taas
    http = request.app.state.http
    upload_filename = file.filename or filename
    files_data = {
        "image": (upload_filename, file_bytes, file.content_type or "application/octet-stream")
    }
    form_data = {"uuid": external_id, "fmt": "multi"}
    if engine_cfg.domain:
        form_data["domain"] = engine_cfg.domain

    resp = await request_with_retry(
        http,
        "POST",
        "/api/v1/jobs",
        headers={"X-API-Key": api_key},
        files=files_data,
        data=form_data,
    )
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text)

    job_data = resp.json()
    await state.set_job_id(request_id, filename, job_data["job_id"])

    return {"status": "ok"}


@router.get(
    "/request_status/{request_id}",
    summary="Report per-image OCR status for a request",
    responses={
        401: {"description": "Missing api-key header."},
        404: {"description": "Request not found in compat state."},
        503: {"description": "taas rate limit could not be absorbed within the retry budget."},
    },
)
async def request_status(request: Request, request_id: str):
    """Return PERO-style per-image processing states for every image in a request.

    Fans out ``GET /api/v1/jobs/{job_id}`` (bounded to four concurrent calls to
    spare the user's query limit) and maps each job onto WAITING (no job yet),
    PROCESSED (taas ``done``), or PROCESSING (everything else, including transient
    taas errors which are deliberately swallowed rather than failing the request).
    """
    api_key = get_api_key(request)
    state = request.app.state.compat_state
    http = request.app.state.http

    job_ids = await state.get_all_job_ids(request_id)
    if not job_ids:
        raise HTTPException(404, "Request not found")

    # Spread the per-image status fan-out so a large request doesn't blow
    # through the user's query limit in one burst.
    sem = asyncio.Semaphore(4)

    async def check_status(fname: str, job_id: str | None) -> tuple[str, dict]:
        if job_id is None:
            return fname, {"state": "WAITING"}
        async with sem:
            resp = await request_with_retry(
                http,
                "GET",
                f"/api/v1/jobs/{job_id}",
                headers={"X-API-Key": api_key},
            )
        if resp.status_code != 200:
            return fname, {"state": "PROCESSING"}
        data = resp.json()
        if data["status"] == "done":
            return fname, {"state": "PROCESSED"}
        return fname, {"state": "PROCESSING"}

    results = await asyncio.gather(*[check_status(f, jid) for f, jid in job_ids.items()])

    return {
        "status": "success",
        "request_status": dict(results),
    }


@router.get(
    "/download_results/{request_id}/{filename}/{format}",
    summary="Download decompressed OCR results for one image",
    responses={
        400: {"description": "Job exists but is not finished processing yet."},
        401: {"description": "Missing api-key header."},
        404: {
            "description": "File/job not found, or no result in the requested format is available."
        },
        502: {"description": "Result artifact could not be downloaded from storage."},
        503: {"description": "taas rate limit could not be absorbed within the retry budget."},
        "4XX": {"description": "Error forwarded verbatim from the taas result-listing call."},
        "5XX": {"description": "Error forwarded verbatim from the taas result-listing call."},
    },
)
async def download_results(
    request: Request,
    request_id: str,
    filename: str,
    format: str,
):
    """Fetch and return the OCR output for one image in ``alto`` (XML) or text form.

    Confirms the taas job is ``done``, lists its results via
    ``GET /api/v1/jobs/{job_id}/result``, picks the entry matching the requested
    format (``alto`` else ``txt``), then downloads the artifact and returns it
    zstd-decompressed with an appropriate text/XML content type. The api-key is
    forwarded as ``X-API-Key`` on the taas calls but the artifact URL is fetched
    unauthenticated.
    """
    api_key = get_api_key(request)
    state = request.app.state.compat_state
    http = request.app.state.http

    job_id = await state.get_job_id(request_id, filename)
    if not job_id:
        raise HTTPException(404, "File not found")

    # Check job status
    resp = await request_with_retry(
        http,
        "GET",
        f"/api/v1/jobs/{job_id}",
        headers={"X-API-Key": api_key},
    )
    if resp.status_code != 200:
        raise HTTPException(404, "Job not found")

    job_data = resp.json()
    if job_data["status"] == "failed":
        raise HTTPException(400, detail="processing failed")
    if job_data["status"] != "done":
        raise HTTPException(400, detail="not processed yet")

    # Fetch the artifact through taas over the internal network. taas's presigned
    # /result URLs are signed for the public MinIO host, which this shim can't reach,
    # so we use the streaming download endpoint instead.
    target_fmt = "alto" if format == "alto" else "txt"
    download_resp = await request_with_retry(
        http,
        "GET",
        f"/api/v1/jobs/{job_id}/result/{target_fmt}/download",
        headers={"X-API-Key": api_key},
    )
    if download_resp.status_code == 404:
        raise HTTPException(404, f"No {format} result available")
    if download_resp.status_code != 200:
        raise HTTPException(502, "Failed to download result")

    dctx = zstandard.ZstdDecompressor()
    decompressed = dctx.decompress(download_resp.content)

    content_type = "text/xml; charset=utf-8" if format == "alto" else "text/plain; charset=utf-8"
    return Response(content=decompressed, media_type=content_type)


@router.get(
    "/get_engines",
    summary="List available OCR engines",
    responses={
        401: {"description": "Missing or invalid api-key header."},
        503: {"description": "taas rate limit could not be absorbed within the retry budget."},
    },
)
async def get_engines(request: Request):
    """List the OCR engines exposed to legacy clients, keyed by display label.

    Validates the ``api-key`` header against taas, then returns the statically
    configured engines (their numeric id and description) from settings. These
    ids are what callers pass as ``engine`` to ``post_processing_request``.
    """
    api_key = get_api_key(request)
    await validate_api_key(request, api_key)
    settings = request.app.state.settings

    engines = {}
    for engine_id, cfg in settings.engines.items():
        engines[cfg.label] = {
            "id": engine_id,
            "description": cfg.description,
        }

    return {"engines": engines}
