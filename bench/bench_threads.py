#!/usr/bin/env python
"""End-to-end wall-clock A/B across CPU counts: baseline vs perf.

For each repo and each CPU count P in {1,2,4,8,16}, pins the process to P cores
(taskset) and runs the sampled images with ocr_threads=1, line_workers=P, then
reports mean seconds/page. Shows both the threading scaling curve and the
perf-vs-baseline delta at each core count.

Usage:
    python bench/bench_threads.py [--n N] [--counts 1,2,4,8,16] [--images DIR]
"""
from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

PARENT = Path(__file__).resolve().parent.parent
REPOS = {"baseline": PARENT / "TuzkaOCR", "perf": PARENT / "TuzkaOCR-perf"}
VENV = PARENT / "TuzkaOCR" / ".venv" / "bin" / "python"
IMAGES_DIR = PARENT / "test-data"


def run_one(repo: str, img: Path, p: int) -> float:
    cores = ",".join(str(i) for i in range(p))
    cmd = ["taskset", "-c", cores, str(VENV), "cli.py", str(img),
           "--out", "/tmp/bench_thr.alto.xml",
           "--ocr-threads", "1", "--line-workers", str(p)]
    t = time.perf_counter()
    subprocess.run(cmd, cwd=str(REPOS[repo]), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)
    return time.perf_counter() - t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3, help="images to sample")
    ap.add_argument("--counts", default="1,2,4,8,16")
    ap.add_argument("--images", default=str(IMAGES_DIR))
    args = ap.parse_args()

    counts = [int(c) for c in args.counts.split(",")]
    images = sorted(Path(args.images).glob("*_image.jpeg"))[: args.n]
    if not images:
        raise SystemExit("no images found")

    # warmup each repo once (compiles numba kernels, fills caches)
    for repo in REPOS:
        run_one(repo, images[0], counts[0])

    print(f"images/sample: {len(images)}   metric: mean seconds/page\n")
    print(f"{'cpus':>5} | {'baseline':>10} | {'perf':>10} | {'speedup':>8}")
    print("-" * 42)
    results = {}
    for p in counts:
        row = {}
        for repo in REPOS:
            times = [run_one(repo, img, p) for img in images]
            row[repo] = sum(times) / len(times)
        results[p] = row
        sp = row["baseline"] / row["perf"] if row["perf"] else 0.0
        print(f"{p:>5} | {row['baseline']:>9.2f}s | {row['perf']:>9.2f}s | {sp:>7.2f}x")
    print("\n(note: end-to-end is OCR-bound by the int8 ConvInteger recognizer;")
    print(" postprocessing gains are partly masked until the model is requantized.)")


if __name__ == "__main__":
    main()
