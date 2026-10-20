"""A fake TuzkaOCR: the same HTTP API, no model (plan 10, D3).

Measures taas, not OCR. Every job "runs" for a configurable time, then succeeds with a
canned ALTO/text result or fails with a configurable HTTP status. Configured by env:

  FAKE_LATENCY_S          mean run time per page in seconds (default 0.05)
  FAKE_LATENCY_DIST       fixed | exp | lognormal (default lognormal)
  FAKE_LATENCY_SIGMA      lognormal sigma (default 0.5)
  FAKE_FAILURE_RATE       share of jobs that fail, 0..1 (default 0)
  FAKE_FAILURE_STATUS     their error_status (default 500: retryable; 422: bad input)
  FAKE_CAPACITY           queued + running jobs before /process answers 503 (default 8)
  FAKE_WORKERS            jobs "running" at once (default 2), like TUZKAOCR_PAGE_WORKERS
  FAKE_RESULT_TTL_S       how long finished jobs are kept (default 600)
  FAKE_SEED               seed the latency/failure RNG for reproducible runs
  TUZKAOCR_API_KEY        required X-API-Key, like the real engine (unset: no auth)
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.responses import PlainTextResponse
from fastapi.security import APIKeyHeader

DOMAINS = ["default", "handwritten", "kramarky", "kurrent"]

LATENCY_S = float(os.environ.get("FAKE_LATENCY_S", "0.05"))
LATENCY_DIST = os.environ.get("FAKE_LATENCY_DIST", "lognormal")
LATENCY_SIGMA = float(os.environ.get("FAKE_LATENCY_SIGMA", "0.5"))
FAILURE_RATE = float(os.environ.get("FAKE_FAILURE_RATE", "0"))
FAILURE_STATUS = int(os.environ.get("FAKE_FAILURE_STATUS", "500"))
CAPACITY = int(os.environ.get("FAKE_CAPACITY", "8"))
WORKERS = int(os.environ.get("FAKE_WORKERS", "2"))
RESULT_TTL_S = float(os.environ.get("FAKE_RESULT_TTL_S", "600"))
API_KEY = os.environ.get("TUZKAOCR_API_KEY", "")

rng = random.Random(os.environ.get("FAKE_SEED"))

ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xsi:schemaLocation="http://www.loc.gov/standards/alto/ns-v4# \
http://www.loc.gov/standards/alto/v4/alto-4-4.xsd">
  <Description><MeasurementUnit>pixel</MeasurementUnit></Description>
  <Layout>
    <Page ID="p_{id}" PHYSICAL_IMG_NR="1" WIDTH="800" HEIGHT="600">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="800" HEIGHT="600">
        <TextBlock ID="b1" HPOS="10" VPOS="10" WIDTH="120" HEIGHT="40">
          <TextLine ID="l1" HPOS="10" VPOS="10" WIDTH="120" HEIGHT="40">
            <String ID="s1" CONTENT="Fake" HPOS="10" VPOS="10" WIDTH="55" HEIGHT="40"/>
            <SP HPOS="65" VPOS="10" WIDTH="10"/>
            <String ID="s2" CONTENT="page" HPOS="75" VPOS="10" WIDTH="55" HEIGHT="40"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>
"""
TXT = "Fake page\n"


@dataclass
class Job:
    id: str
    fmt: str
    status: str = "queued"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    error_status: int | None = None
    done_at: float = 0.0  # monotonic, for the TTL sweep


jobs: dict[str, Job] = {}
workers = asyncio.Semaphore(WORKERS)
app = FastAPI(title="fake-tuzkaocr", version="fake-1.0")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_key(key: str | None = Security(_api_key_header)) -> None:
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _latency() -> float:
    if LATENCY_DIST == "fixed":
        return LATENCY_S
    if LATENCY_DIST == "exp":
        return rng.expovariate(1 / LATENCY_S) if LATENCY_S > 0 else 0.0
    # lognormal with the configured mean: mu = ln(mean) - sigma^2 / 2
    mu = math.log(max(LATENCY_S, 1e-6)) - LATENCY_SIGMA**2 / 2
    return rng.lognormvariate(mu, LATENCY_SIGMA)


def _sweep() -> None:
    now = time.monotonic()
    for job_id in [j.id for j in jobs.values() if j.done_at and now - j.done_at > RESULT_TTL_S]:
        del jobs[job_id]


async def _run(job: Job) -> None:
    async with workers:
        job.status, job.started_at = "running", datetime.now(UTC)
        await asyncio.sleep(_latency())
        if rng.random() < FAILURE_RATE:
            job.status, job.error, job.error_status = "failed", "fake failure", FAILURE_STATUS
        else:
            job.status = "done"
        job.finished_at = datetime.now(UTC)
        job.done_at = time.monotonic()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/models", dependencies=[Depends(_require_key)])
async def models() -> dict[str, object]:
    return {
        "selectable_via_domain": DOMAINS,
        "capabilities": {d: ["alto", "txt"] for d in DOMAINS},
    }


@app.post("/api/v1/process", dependencies=[Depends(_require_key)])
async def process(
    image: UploadFile = File(...),
    domain: str | None = Form(None),
    fmt: str | None = Form(None),
    role_classifier: bool | None = Form(None),
    source_identifier: str | None = Form(None),
    alto_profile: Literal["ndk", "basic"] | None = Form(None),
    physical_img_nr: int | None = Form(None),
) -> dict[str, str]:
    _sweep()
    if domain is not None and domain not in DOMAINS:
        raise HTTPException(status_code=422, detail=f"Unknown domain '{domain}'")
    if sum(1 for j in jobs.values() if j.status in ("queued", "running")) >= CAPACITY:
        raise HTTPException(status_code=503, detail=f"queue full (>= {CAPACITY})")
    await image.read()  # consume the upload like the real engine does
    job = Job(id=uuid.uuid4().hex, fmt=fmt or "multi")
    jobs[job.id] = job
    asyncio.get_running_loop().create_task(_run(job))
    return {"job_id": job.id, "status": "queued"}


@app.get("/api/v1/status/{job_id}", dependencies=[Depends(_require_key)])
async def status(job_id: str) -> dict[str, object]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.id,
        "status": job.status,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "mean_conf": 0.9 if job.status == "done" else None,
        "n_lines": 1 if job.status == "done" else None,
        "error": job.error,
        "error_status": job.error_status if job.status == "failed" else None,
    }


@app.get("/api/v1/result/{job_id}", dependencies=[Depends(_require_key)])
async def result(job_id: str, which: str | None = None) -> PlainTextResponse:
    if which is not None and which not in ("alto", "txt"):
        raise HTTPException(status_code=400, detail=f"Unknown which '{which}'")
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == "failed":
        raise HTTPException(status_code=job.error_status or 500, detail=job.error)
    if job.status != "done":
        raise HTTPException(status_code=202, detail=f"Job status: {job.status}")
    if (which or ("txt" if job.fmt == "txt" else "alto")) == "txt":
        return PlainTextResponse(TXT, media_type="text/plain; charset=utf-8")
    return PlainTextResponse(ALTO.format(id=job.id), media_type="text/xml; charset=utf-8")
