#!/usr/bin/env python
"""Benchmark the modern taas API through `taas_client`, over the same corpus.

The counterpart to `legacy_proarc.py`. Where the legacy client blocks on one
page at a time and polls a download endpoint every 3 s, this submits every page up
front and lets the WebSocket push results back:

* **All pages in flight at once** — the queue, not the client, decides the pacing.
* **No polling.** One `/ws` connection carries every state change; per page the only
  HTTP calls are the submit and the presigned result fetch(es).
* **Results arrive decompressed** — the client fetches the presigned URL and unwraps
  zstd inside the callback, so what's timed is bytes-in-hand, same as legacy.

Reading the numbers: `wall_seconds` / `pages_per_minute` are directly comparable to
the legacy run. Per-page `latency` is not — by default it includes time queued behind
the other pages, whereas ProArc's sequential loop never has a queue to wait in.

`--serial` paces this client like ProArc (submit one page, block for its result, then
the next). Nothing queues, so *there* per-page latency is comparable and the gap is
pure protocol overhead. See README.md, "Two comparisons".

Usage:
    python bench/api/new_client.py --n 10 --images test-data
    python bench/api/new_client.py --n 10 --serial
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "clients" / "python"))

from taas_client import JobEvent, JobResult, JobStatus, TaasClient  # noqa: E402

from common import (  # noqa: E402
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


class Collector:
    """Correlates client callbacks (WebSocket thread) back to submitted pages."""

    def __init__(self, out_dir: Path | None = None) -> None:
        self._out_dir = out_dir
        self._lock = threading.Lock()
        # keyed by the external_id each page was submitted with
        self._pages: dict[str, tuple[str, float]] = {}
        self._timings: list[PageTiming] = []
        self.first_result_at: float | None = None

    def submitted(self, key: str, name: str, at: float) -> None:
        with self._lock:
            self._pages[key] = (name, at)

    def on_result(self, result: JobResult) -> None:
        with self._lock:
            key = result.external_id or str(result.job_id)
            name, started = self._pages.pop(key, (key, 0.0))

        alto = result.alto or b""
        txt = result.txt or b""
        error: str | None = None

        # Same contract as the legacy side: the client must finish holding both
        # decompressed artifacts. Write them before stopping the clock.
        try:
            store_artifacts(self._out_dir, name, alto, txt)
        except MissingArtifact as exc:
            error = str(exc)

        finished = time.monotonic()

        with self._lock:
            if self.first_result_at is None:
                self.first_result_at = finished

            self._timings.append(
                PageTiming(
                    name=name,
                    started=started,
                    finished=finished,
                    ok=error is None,
                    # 1 submit + one presigned fetch per artifact returned.
                    http_requests=1 + bool(alto) + bool(txt),
                    polls=0,
                    alto_bytes=len(alto),
                    txt_bytes=len(txt),
                    error=error,
                )
            )

    def on_error(self, event: JobEvent) -> None:
        finished = time.monotonic()

        with self._lock:
            key = event.external_id or str(event.job_id)
            name, started = self._pages.pop(key, (key, finished))
            self._timings.append(
                PageTiming(
                    name=name,
                    started=started,
                    finished=finished,
                    ok=False,
                    http_requests=1,
                    error=event.error or "job failed",
                )
            )

    def abandon_unresolved(self, reason: str) -> None:
        """Fail whatever never came back, so the summary accounts for every page."""
        finished = time.monotonic()

        with self._lock:
            for name, started in self._pages.values():
                self._timings.append(
                    PageTiming(
                        name=name,
                        started=started,
                        finished=finished,
                        ok=False,
                        http_requests=1,
                        error=reason,
                    )
                )
            self._pages.clear()

    @property
    def timings(self) -> list[PageTiming]:
        with self._lock:
            return list(self._timings)


def _submit_failed(key: str, exc: Exception) -> JobEvent:
    """A failure event for a page whose submit was rejected (so it has no job id)."""
    return JobEvent(
        job_id=UUID(int=0), status=JobStatus.FAILED, external_id=key, error=f"submit: {exc}"
    )


def submit_all(
    client: TaasClient, collector: Collector, images: list[Path], concurrency: int
) -> list[str]:
    """Submit every page, recording when each went out. Returns submit errors."""
    errors: list[str] = []
    errors_lock = threading.Lock()

    def submit_one(page: Path) -> None:
        key = str(uuid4())
        started = time.monotonic()
        # Register before the POST returns: a fast job can be pushed back over the
        # WebSocket while submit() is still unwinding. The key comes back as the
        # result's external_id.
        collector.submitted(key, page.name, started)

        try:
            client.submit(page, external_id=key)
        except Exception as exc:
            collector.on_error(_submit_failed(key, exc))
            with errors_lock:
                errors.append(f"{page.name}: {exc}")

    if concurrency == 1:
        for page in images:
            submit_one(page)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(submit_one, images))

    return errors


def submit_serially(
    client: TaasClient, collector: Collector, images: list[Path], timeout: float
) -> list[str]:
    """One page at a time: submit, block until its result lands, then the next.

    This is the apples-to-apples counterpart to ProArc's serial loop. Nothing ever
    queues behind anything else, so per-page latency is the protocol's true
    round-trip cost and is directly comparable to the legacy run's — unlike the
    all-in-flight mode, where latency absorbs queue time.

    `client.wait()` works per page here because only one job is ever pending.
    """
    errors: list[str] = []

    for page in images:
        key = str(uuid4())
        collector.submitted(key, page.name, time.monotonic())

        try:
            client.submit(page, external_id=key)
        except Exception as exc:
            collector.on_error(_submit_failed(key, exc))
            errors.append(f"{page.name}: {exc}")
            continue

        if not client.wait(timeout=timeout):
            collector.abandon_unresolved(f"no result within {timeout:.0f}s")
            errors.append(f"{page.name}: timed out")

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--fmt", default="multi", choices=("alto", "txt", "multi"))
    parser.add_argument("--domain", default=None, help="engine domain to route to")
    parser.add_argument(
        "--submit-concurrency",
        type=int,
        default=1,
        help="parallel submit POSTs; 1 matches the documented usage",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="one page at a time (wait for each result) — comparable to ProArc's loop",
    )
    args = parser.parse_args()

    images = discover_images(args.images, args.n, args.seed)
    api_key = resolve_api_key(args, "bench-new")

    banner(
        "taas API (taas_client, WebSocket push)",
        images,
        taas=args.taas_url,
        fmt=args.fmt,
        domain=args.domain,
        pacing="serial (1 page in flight)" if args.serial else "all pages in flight",
        submit_concurrency=1 if args.serial else args.submit_concurrency,
    )

    collector = Collector(out_dir=(args.out_dir / "new") if args.out_dir else None)
    client = TaasClient(
        url=args.taas_url,
        api_key=api_key,
        on_result=collector.on_result,
        on_error=collector.on_error,
        fmt=args.fmt,
        domain=args.domain,
    )

    client.start()
    # The WebSocket connects on its own thread; give it a moment so the first
    # results are pushed rather than replayed through the catch-up window.
    time.sleep(1.0)

    started = time.monotonic()
    submitted_at = started
    submit_errors: list[str] = []

    try:
        if args.serial:
            submit_errors = submit_serially(client, collector, images, args.timeout)
            submitted_at = time.monotonic()
        else:
            submit_errors = submit_all(client, collector, images, args.submit_concurrency)
            submitted_at = time.monotonic()

            if not client.wait(timeout=args.timeout):
                collector.abandon_unresolved(f"no result within {args.timeout:.0f}s")
    finally:
        client.stop()

    wall = time.monotonic() - started
    first = collector.first_result_at

    run = BenchRun(
        mode="new (taas API / WebSocket client)" + (" — serial" if args.serial else ""),
        concurrency=1 if args.serial else len(images),
        wall_seconds=wall,
        timings=collector.timings,
        server=fetch_server_timings(args.taas_url, api_key),
        notes={
            "pacing": "serial" if args.serial else "all-in-flight",
            # In serial mode the submit phase *is* the whole run, so this equals wall.
            "submit_phase_seconds": round(submitted_at - started, 2),
            "time_to_first_result": round(first - started, 2) if first else None,
            "submit_concurrency": 1 if args.serial else args.submit_concurrency,
            "fmt": args.fmt,
            "requests_per_page": "1 submit + 1 fetch per artifact (no polling)",
            "submit_errors": submit_errors[:5],
        },
    )

    summary = summarize(run)
    print_report(summary)
    for key, value in run.notes.items():
        print(f"  {key:<18} {value}")
    write_json(args.json, summary, run)


if __name__ == "__main__":
    main()
