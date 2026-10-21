#!/usr/bin/env python3
"""taas client demo: the same directory of images, OCR'd both ways.

1. Per page — submit every image as its own job, labelled with your own page id
   (`external_id`, here the file name without extension); results arrive over the
   WebSocket as each page finishes, carrying that label. Best for interactive use and
   small sets.
2. As one batch — pack the same images into a .tar.zst and submit it once, labelled
   with your id for the whole batch (here the directory name). Watch the progress
   events, then download one archive with every page's output; its manifest maps each
   page back by its file name (`source_name`). Best for large sets.

    python examples/demo.py --url http://localhost:8080 --api-key KEY test-data/
    python examples/demo.py ... --domain kramarky --outputs alto,txt \\
        --options '{"alto_profile": "basic"}'
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from pathlib import Path

from taas_client import BatchProgress, JobEvent, JobResult, TaasClient

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".jp2"}


def list_domains(client: TaasClient) -> None:
    print("Domains you can submit to:")
    for d in client.domains():
        options = sorted((d.options_schema or {}).get("properties", {}))
        outputs = ",".join(d.outputs)
        print(f"  {d.name:<12} outputs={outputs:<12} options={', '.join(options) or '-'}")


def per_page(client: TaasClient, images: list[Path], out: Path) -> float:
    """Submit each image as a job; the WebSocket callbacks write the results."""
    print(f"\n== Per page: {len(images)} job(s)")
    started = time.monotonic()
    for image in images:
        client.submit(image, external_id=image.stem)  # your id for this page
    if not client.wait(timeout=3600):
        print("  timed out waiting for results")
    elapsed = time.monotonic() - started
    print(f"  done in {elapsed:.1f}s, results in {out}")
    return elapsed


def as_batch(client: TaasClient, images: list[Path], label: str, out: Path) -> float:
    """Submit the same images as one batch and download one results archive. The
    client packs them into a .tar.zst; a directory or a ready archive works too."""
    print("\n== As one batch")
    started = time.monotonic()
    batch = client.submit_batch(images, external_id=label)  # your id for the batch
    print(f"  batch {batch.batch_id} ({batch.external_id}): {batch.total} page(s)", end="")
    print(f", skipped {', '.join(batch.skipped)}" if batch.skipped else "")

    status = client.wait_batch(batch.batch_id, timeout=3600)
    print(f"  finished: {status.done} done, {status.failed} failed")

    results = client.download_batch_results(batch.batch_id, out, extract=True)
    elapsed = time.monotonic() - started
    print(f"  done in {elapsed:.1f}s, archive {results.archive}, extracted to {results.directory}")
    for entry in (results.manifest or [])[:5]:
        files = ", ".join(entry.get("outputs", {}).values()) or entry.get("error")
        print(f"    {entry['source_name']}: {entry['status']} -> {files}")
    if results.manifest and len(results.manifest) > 5:
        print(f"    … {len(results.manifest) - 5} more in manifest.json")
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("images", type=Path, help="directory of page images")
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--domain", help="domain to OCR in (default: the server's default)")
    parser.add_argument("--outputs", help="comma-separated outputs, e.g. alto,txt")
    parser.add_argument("--options", help="engine options as a JSON object")
    parser.add_argument("--out", type=Path, help="where results go (default: a temp dir)")
    args = parser.parse_args()

    images = sorted(p for p in args.images.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        parser.error(f"no images in {args.images}")
    out = args.out or Path(tempfile.mkdtemp(prefix="taas-demo-"))
    page_dir = out / "per-page"
    page_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    def on_result(result: JobResult) -> None:
        name = result.external_id or str(result.job_id)  # the label we submitted it with
        if result.alto:
            (page_dir / f"{name}.xml").write_bytes(result.alto)
        if result.txt:
            (page_dir / f"{name}.txt").write_bytes(result.txt)
        with lock:
            print(f"  page {name} done")

    def on_error(event: JobEvent) -> None:
        with lock:
            print(f"  page {event.external_id or event.job_id} FAILED: {event.error}")

    def on_batch(progress: BatchProgress) -> None:
        with lock:
            print(f"  batch progress: {progress.done + progress.failed}/{progress.total}")

    client = TaasClient(
        url=args.url,
        api_key=args.api_key,
        on_result=on_result,
        on_error=on_error,
        on_batch=on_batch,
        domain=args.domain,
        outputs=args.outputs.split(",") if args.outputs else None,
        options=json.loads(args.options) if args.options else None,
    )
    client.start()
    try:
        list_domains(client)
        page_s = per_page(client, images, page_dir)
        batch_s = as_batch(client, images, args.images.resolve().name, out / "batch")
    finally:
        client.stop()

    print(f"\n{len(images)} page(s): per page {page_s:.1f}s, as one batch {batch_s:.1f}s")


if __name__ == "__main__":
    main()
