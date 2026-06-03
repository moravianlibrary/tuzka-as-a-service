#!/usr/bin/env python3
"""Test taas Python client library with real TuzkaOCR backend."""

import os
import sys
from pathlib import Path
from uuid import uuid4

from taas_client import JobEvent, JobResult, TaasClient

TAAS_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
API_KEY = sys.argv[2] if len(sys.argv) > 2 else None
IMAGE_DIR = sys.argv[3] if len(sys.argv) > 3 else None
FMT = os.environ.get("FMT", "multi")

if not API_KEY or not IMAGE_DIR:
    print(f"Usage: {sys.argv[0]} <taas_url> <api_key> <image_dir>")
    sys.exit(1)

results_received = 0
errors_received = 0


def handle_result(result: JobResult):
    global results_received
    results_received += 1
    print(f"[{results_received}] Result for {result.uuid}")
    if result.alto:
        out = Path(f"output/{result.uuid}.xml")
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(result.alto)
        print(f"    ALTO: {len(result.alto)} bytes -> {out}")
    if result.txt:
        out = Path(f"output/{result.uuid}.txt")
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(result.txt)
        print(f"    TXT:  {len(result.txt)} bytes -> {out}")


def handle_error(event: JobEvent):
    global errors_received
    errors_received += 1
    print(f"[ERROR] Job {event.uuid} failed: {event.error}")


client = TaasClient(
    url=TAAS_URL,
    api_key=API_KEY,
    on_result=handle_result,
    on_error=handle_error,
    fmt=FMT,
)
client.start()

images = sorted(Path(IMAGE_DIR).glob("*"))
images = [p for p in images if p.suffix.lower() in {".tif", ".tiff", ".jpg", ".jpeg", ".png"}]
print(f"Submitting {len(images)} images...")

for img in images:
    uid = uuid4()
    client.submit(img, uuid=uid)
    print(f"  Submitted {img.name} as {uid}")

print("Waiting for all results...")
client.wait(timeout=300)
client.stop()

print(f"\nDone: {results_received} results, {errors_received} errors")
if errors_received > 0:
    sys.exit(1)
