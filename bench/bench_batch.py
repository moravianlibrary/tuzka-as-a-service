#!/usr/bin/env python
"""Batch A/B: one model load per run, all images processed in a single process.

This is the realistic throughput / API case — numba import + model load are paid
once, not per file, so post-processing wins are no longer masked by per-process
overhead. Also compares ALTO word content across the whole corpus (end-to-end
text parity, baseline vs perf).

Usage:
    python bench/bench_batch.py [--n N] [--line-workers W]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path

PARENT = Path(__file__).resolve().parent.parent
REPOS = {"baseline": PARENT / "TuzkaOCR", "perf": PARENT / "TuzkaOCR-perf"}
VENV = PARENT / "TuzkaOCR" / ".venv" / "bin" / "python"
IMAGES = PARENT / "test-data"


def run_batch(repo: str, out_dir: Path, line_workers: int) -> float:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(VENV), "cli.py", str(IMAGES), "--batch", "--out-dir", str(out_dir),
           "--workers", "1", "--ocr-threads", "1", "--line-workers", str(line_workers),
           "--format", "alto"]
    t = time.perf_counter()
    subprocess.run(cmd, cwd=str(REPOS[repo]), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)
    return time.perf_counter() - t


def words(path: Path) -> list[str]:
    return sorted(re.findall(r'CONTENT="([^"]*)"', path.read_text(encoding="utf-8")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--line-workers", type=int, default=8)
    args = ap.parse_args()

    imgs = sorted(IMAGES.glob("*_image.jpeg"))[: args.n]
    n = len(imgs)
    outs = {}
    print(f"images: {n}   workers=1 (1 model load)   line_workers={args.line_workers}\n")
    for repo in REPOS:
        out = Path(f"/tmp/batch_{repo}")
        secs = run_batch(repo, out, args.line_workers)
        outs[repo] = out
        print(f"{repo:>9}: {secs:7.1f}s total   {secs / n:5.2f}s/page")

    # end-to-end text parity across the corpus
    mismatch = 0
    for img in imgs:
        bl = outs["baseline"] / f"{img.stem}.alto.xml"
        pf = outs["perf"] / f"{img.stem}.alto.xml"
        if not (bl.exists() and pf.exists()):
            continue
        if words(bl) != words(pf):
            mismatch += 1
            print(f"  TEXT DIFF: {img.name}")
    print(f"\nend-to-end word-content mismatches: {mismatch}/{n}")


if __name__ == "__main__":
    main()
