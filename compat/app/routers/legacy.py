import asyncio
from uuid import uuid4

import zstandard
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from starlette.responses import Response

router = APIRouter()


def get_api_key(request: Request) -> str:
    key = request.headers.get("api-key")
    if not key:
        raise HTTPException(401, "Missing api-key header")
    return key


@router.post("/post_processing_request")
async def post_processing_request(request: Request):
    api_key = get_api_key(request)
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


@router.get("/get_status")
async def get_status(request: Request, request_id: str):
    get_api_key(request)
    state = request.app.state.compat_state
    req = await state.get_request(request_id)
    if not req:
        raise HTTPException(400, "Unknown request_id")
    return {"status": "success", "request_id": request_id}


@router.post("/upload_image/{request_id}/{filename}")
async def upload_image(
    request: Request,
    request_id: str,
    filename: str,
    file: UploadFile = File(...),
):
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
    files_data = {"image": (upload_filename, file_bytes, file.content_type or "application/octet-stream")}
    form_data = {"uuid": external_id, "fmt": "multi"}
    if engine_cfg.domain:
        form_data["domain"] = engine_cfg.domain

    resp = await http.post(
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


@router.get("/request_status/{request_id}")
async def request_status(request: Request, request_id: str):
    api_key = get_api_key(request)
    state = request.app.state.compat_state
    http = request.app.state.http

    job_ids = await state.get_all_job_ids(request_id)
    if not job_ids:
        raise HTTPException(404, "Request not found")

    async def check_status(fname: str, job_id: str | None) -> tuple[str, dict]:
        if job_id is None:
            return fname, {"state": "WAITING"}
        resp = await http.get(
            f"/api/v1/jobs/{job_id}",
            headers={"X-API-Key": api_key},
        )
        if resp.status_code != 200:
            return fname, {"state": "PROCESSING"}
        data = resp.json()
        if data["status"] == "done":
            return fname, {"state": "PROCESSED"}
        return fname, {"state": "PROCESSING"}

    results = await asyncio.gather(
        *[check_status(f, jid) for f, jid in job_ids.items()]
    )

    return {
        "status": "success",
        "request_status": dict(results),
    }


@router.get("/download_results/{request_id}/{filename}/{format}")
async def download_results(
    request: Request,
    request_id: str,
    filename: str,
    format: str,
):
    api_key = get_api_key(request)
    state = request.app.state.compat_state
    http = request.app.state.http

    job_id = await state.get_job_id(request_id, filename)
    if not job_id:
        raise HTTPException(404, "File not found")

    # Check job status
    resp = await http.get(
        f"/api/v1/jobs/{job_id}",
        headers={"X-API-Key": api_key},
    )
    if resp.status_code != 200:
        raise HTTPException(404, "Job not found")

    job_data = resp.json()
    if job_data["status"] != "done":
        raise HTTPException(400, detail="not processed yet")

    # Get result URLs
    resp = await http.get(
        f"/api/v1/jobs/{job_id}/result",
        headers={"X-API-Key": api_key},
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)

    results = resp.json()["results"]

    # Find matching format
    target_fmt = "alto" if format == "alto" else "txt"
    result_entry = None
    for r in results:
        if r["fmt"] == target_fmt:
            result_entry = r
            break

    if not result_entry:
        raise HTTPException(404, f"No {format} result available")

    # Download and decompress
    download_resp = await http.get(result_entry["url"])
    if download_resp.status_code != 200:
        raise HTTPException(502, "Failed to download result")

    dctx = zstandard.ZstdDecompressor()
    decompressed = dctx.decompress(download_resp.content)

    content_type = "text/xml; charset=utf-8" if format == "alto" else "text/plain; charset=utf-8"
    return Response(content=decompressed, media_type=content_type)


@router.get("/get_engines")
async def get_engines(request: Request):
    get_api_key(request)
    settings = request.app.state.settings

    engines = {}
    for engine_id, cfg in settings.engines.items():
        engines[cfg.label] = {
            "id": engine_id,
            "description": cfg.description,
        }

    return {"engines": engines}
