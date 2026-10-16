#!/usr/bin/env python
"""Benchmark the legacy PERO API (via `compat`) exactly the way ProArc drives it.

This is a faithful port of ProArc's `PeroOcrProcessor` — see
proarc-common/src/main/java/cz/cas/lib/proarc/common/process/external/PeroOcrProcessor.java
and its caller `PeroProcess.generateMultipleAlto()`. The details that dominate the
numbers are all deliberate, not accidents of the port:

* **One request per page.** `process()` opens a fresh `post_processing_request`
  carrying a single image, even though the API accepts a whole map of them.
* **Strictly sequential.** `generateMultipleAlto()` is a plain `for` loop over the
  folder and `process()` blocks until the page is PROCESSED, so pages never overlap.
  `--concurrency` exists to price what ProArc *gives up*, not to model what it does.
* **A fresh HttpClient per page** (`HttpClients.createDefault()` inside `process()`),
  so no connection or TLS session is reused across pages.
* **Polling by download.** There is no status call in the hot loop: it re-requests
  `download_results/.../txt` every 3 s and treats a 4xx whose `message` contains
  "not processed yet" as "keep waiting".
* **Two downloads per page** — txt first (that's the poll), then alto.
* **Filename is the basename before the first dot** (`getName().split("\\.")[0]`),
  while the multipart part keeps the real filename, extension and all.

Usage:
    python bench/api/legacy_proarc.py --n 10 --images test-data
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx

from common import (
    DEFAULT_COMPAT_URL,
    BenchRun,
    MissingArtifact,
    PageTiming,
    add_common_args,
    banner,
    discover_images,
    fetch_server_timings,
    print_report,
    resolve_api_key,
    store_artifacts,
    summarize,
    write_json,
)

# PeroOcrProcessor: TimeUnit.SECONDS.sleep(3) between download attempts, and
# TimeUnit.SECONDS.sleep(15) while the request itself is not yet "success".
PROARC_POLL_SECONDS = 3.0
PROARC_REQUEST_WAIT_SECONDS = 15.0


@dataclass(frozen=True)
class LegacyConfig:
    compat_url: str
    api_key: str
    engine: int
    poll_seconds: float
    timeout: float
    reuse_connections: bool
    out_dir: Path | None


class PageFailed(Exception):
    """A page could not be OCR'd — carries the message ProArc would have logged."""


def legacy_filename(path: Path) -> str:
    """Reproduce ProArc's `new File(imagePath).getName().split("\\.")[0]`.

    Note this splits on the *first* dot, so `sig.0001.jp2` becomes `sig` — two such
    pages in one folder would collide. ProArc has the same hazard; we surface it.
    """
    return path.name.split(".")[0]


def post_processing_request(http: httpx.Client, cfg: LegacyConfig, name: str) -> str:
    """Open a request for a single page and wait until it reports success."""
    body = {"engine": cfg.engine, "images": {name: None}}
    resp = http.post("/post_processing_request", json=body, headers={"api-key": cfg.api_key})
    if resp.status_code >= 400:
        raise PageFailed(f"post_processing_request HTTP {resp.status_code}: {resp.text[:200]}")

    payload = resp.json()
    deadline = time.monotonic() + cfg.timeout

    while payload.get("status") != "success":
        if time.monotonic() > deadline:
            raise PageFailed("request never reached status=success")

        time.sleep(PROARC_REQUEST_WAIT_SECONDS)
        status = http.get(
            "/get_status",
            params={"request_id": payload["request_id"]},
            headers={"api-key": cfg.api_key},
        )
        if status.status_code != 200:
            raise PageFailed(f"get_status HTTP {status.status_code}")
        payload = status.json()

    return str(payload["request_id"])


def upload_image(http: httpx.Client, cfg: LegacyConfig, request_id: str, page: Path) -> None:
    """Upload the page under its legacy name.

    ProArc's `addBinaryBody("file", imageFile)` sends `application/octet-stream` and the
    real filename — the MIME type it computes in `getContentType()` is never used. taas
    gates on the multipart filename's extension, so that part matters and the type does not.
    """
    name = legacy_filename(page)
    files = {"file": (page.name, page.read_bytes(), "application/octet-stream")}
    resp = http.post(
        f"/upload_image/{request_id}/{name}",
        files=files,
        headers={"api-key": cfg.api_key},
    )
    if resp.status_code >= 400:
        raise PageFailed(f"upload_image HTTP {resp.status_code}: {resp.text[:200]}")


def download_result(
    http: httpx.Client, cfg: LegacyConfig, request_id: str, name: str, fmt: str
) -> bytes | None:
    """Fetch one format; `None` means "not processed yet", the retry signal."""
    resp = http.get(
        f"/download_results/{request_id}/{name}/{fmt}",
        headers={"api-key": cfg.api_key},
    )
    if resp.status_code == 200:
        return resp.content

    if resp.status_code < 400:
        raise PageFailed(f"download_results/{fmt} unexpected HTTP {resp.status_code}")

    # The legacy contract is an error envelope with a `message`; ProArc reads it
    # blind and NPEs on anything else, so a missing key is worth reporting.
    try:
        message = resp.json()["message"]
    except Exception:
        raise PageFailed(
            f"download_results/{fmt} HTTP {resp.status_code} without a "
            f'"message" field (ProArc would throw here): {resp.text[:200]}'
        ) from None

    if "not processed yet" in message:
        return None

    raise PageFailed(f"download_results/{fmt} HTTP {resp.status_code}: {message}")


def run_page(page: Path, cfg: LegacyConfig, shared: httpx.Client | None) -> PageTiming:
    """Run ProArc's whole per-page dance and time it."""
    name = legacy_filename(page)
    started = time.monotonic()
    requests = 0
    polls = 0

    # ProArc builds a new HttpClient inside process(), i.e. once per page.
    http = shared or httpx.Client(base_url=cfg.compat_url, timeout=120.0)

    try:
        request_id = post_processing_request(http, cfg, name)
        requests += 1

        upload_image(http, cfg, request_id, page)
        requests += 1

        deadline = started + cfg.timeout
        txt = None

        while txt is None:
            txt = download_result(http, cfg, request_id, name, "txt")
            requests += 1
            polls += 1

            if txt is not None:
                break

            if time.monotonic() > deadline:
                raise PageFailed(f"timed out after {cfg.timeout:.0f}s ({polls} polls)")

            time.sleep(cfg.poll_seconds)

        alto = download_result(http, cfg, request_id, name, "alto")
        requests += 1
        if alto is None:
            raise PageFailed("alto reported not-processed after txt was ready")

        # ProArc writes both files to disk before moving on; so do we, inside the timing.
        store_artifacts(cfg.out_dir, page.name, alto, txt)

        return PageTiming(
            name=page.name,
            started=started,
            finished=time.monotonic(),
            ok=True,
            http_requests=requests,
            polls=polls,
            alto_bytes=len(alto),
            txt_bytes=len(txt),
        )

    except (PageFailed, MissingArtifact, httpx.HTTPError) as exc:
        return PageTiming(
            name=page.name,
            started=started,
            finished=time.monotonic(),
            ok=False,
            http_requests=requests,
            polls=polls,
            error=str(exc),
        )

    finally:
        if shared is None:
            http.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--compat-url", default=DEFAULT_COMPAT_URL)
    parser.add_argument("--engine", type=int, default=1, help="legacy engine id")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="pages in flight; 1 is what ProArc actually does",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=PROARC_POLL_SECONDS,
        help="download-retry interval (ProArc hardcodes 3)",
    )
    parser.add_argument(
        "--reuse-connections",
        action="store_true",
        help="share one HTTP client across pages (ProArc does not)",
    )
    args = parser.parse_args()

    images = discover_images(args.images, args.n, args.seed)
    api_key = resolve_api_key(args, "bench-legacy")

    cfg = LegacyConfig(
        compat_url=args.compat_url.rstrip("/"),
        api_key=api_key,
        engine=args.engine,
        poll_seconds=args.poll_seconds,
        timeout=args.timeout,
        reuse_connections=args.reuse_connections,
        out_dir=(args.out_dir / "legacy") if args.out_dir else None,
    )

    banner(
        "legacy PERO API (ProArc PeroOcrProcessor replay)",
        images,
        compat=cfg.compat_url,
        engine=cfg.engine,
        concurrency=args.concurrency,
        poll_seconds=cfg.poll_seconds,
        connections="shared" if cfg.reuse_connections else "fresh-per-page",
    )

    shared = httpx.Client(base_url=cfg.compat_url, timeout=120.0) if cfg.reuse_connections else None
    started = time.monotonic()

    try:
        if args.concurrency == 1:
            timings = [run_page(p, cfg, shared) for p in images]
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                timings = list(pool.map(lambda p: run_page(p, cfg, shared), images))
    finally:
        if shared:
            shared.close()

    run = BenchRun(
        mode="legacy (compat / ProArc replay)",
        concurrency=args.concurrency,
        wall_seconds=time.monotonic() - started,
        timings=timings,
        server=fetch_server_timings(args.taas_url, api_key),
        notes={
            "poll_seconds": cfg.poll_seconds,
            "connections": "shared" if cfg.reuse_connections else "fresh-per-page",
            "requests_per_page": "1 open + 1 upload + N polls + 1 alto",
        },
    )

    summary = summarize(run)
    print_report(summary)
    write_json(args.json, summary, run)


if __name__ == "__main__":
    main()
