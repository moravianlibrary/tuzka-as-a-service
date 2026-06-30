#!/usr/bin/env python
"""Confound-controlled variant bench: ONE thread scenario, rotating interleave.

Each round runs all variants once, but the order is rotated per round (Latin-
square style) so no variant is always first-on-a-cool-CPU. Median per variant
across rounds removes thermal drift + run-order bias — the flaw in bench_matrix.py.

Single scenario: ocr_threads=1, line_workers=1 (the 1-CPU-engine config).

Usage: python bench/bench_matrix_clean.py [--rounds 5] [--lw 1]
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import time
from pathlib import Path

PARENT = Path(__file__).resolve().parent.parent
PERF = PARENT / "TuzkaOCR-perf"
VENV = PARENT / "TuzkaOCR" / ".venv" / "bin" / "python"
IMAGES = PARENT / "test-data"
OUT = PARENT / "bench" / "matrix_clean_out"
RESULTS = PARENT / "bench" / "RESULTS_MATRIX_CLEAN.md"

VARIANTS = ["main", "bench-ctc", "bench-median", "bench-numba", "bench-small-numpy"]
LABEL = {"main": "base", "bench-ctc": "ctc", "bench-median": "median",
         "bench-numba": "numba", "bench-small-numpy": "small-numpy"}


def git_checkout(b: str) -> None:
    subprocess.run(["git", "-C", str(PERF), "checkout", b], check=True,
                   capture_output=True, text=True)


def run_batch(v: str, lw: int) -> float:
    out_dir = OUT / v
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(VENV), "cli.py", str(IMAGES), "--batch", "--out-dir", str(out_dir),
           "--workers", "1", "--ocr-threads", "1", "--line-workers", str(lw),
           "--format", "alto"]
    t = time.perf_counter()
    subprocess.run(cmd, cwd=str(PERF), capture_output=True, text=True, check=True)
    return time.perf_counter() - t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--lw", type=int, default=1)
    args = ap.parse_args()
    n = len(sorted(IMAGES.glob("*_image.jpeg")))
    samples: dict[str, list[float]] = {v: [] for v in VARIANTS}

    try:
        for r in range(args.rounds):
            order = VARIANTS[r % len(VARIANTS):] + VARIANTS[:r % len(VARIANTS)]
            for v in order:
                git_checkout(v)
                dt = run_batch(v, args.lw)
                samples[v].append(dt)
                print(f"round {r+1}/{args.rounds}  {LABEL[v]:<11} {dt:7.1f}s "
                      f"({dt/n:.3f}s/page)", flush=True)
    finally:
        git_checkout("main")

    base_med = statistics.median(samples["main"])
    lines = [f"# Clean variant bench — interleaved, median of {args.rounds} rounds\n",
             f"Corpus: {n} pages · 1 model load/batch · `ocr_threads=1 line_workers={args.lw}` · "
             f"rotating order per round (thermal/order-controlled)\n",
             "| variant | median s/page | min | max | vs base |",
             "|---|---|---|---|---|"]
    for v in VARIANTS:
        med = statistics.median(samples[v])
        lo, hi = min(samples[v]), max(samples[v])
        delta = "—" if v == "main" else f"{(med - base_med)/base_med*100:+.1f}%"
        lines.append(f"| {LABEL[v]} | {med/n:.3f} | {lo/n:.3f} | {hi/n:.3f} | {delta} |")
    lines.append(f"\nRaw per-round s/page samples:")
    for v in VARIANTS:
        lines.append(f"- {LABEL[v]}: " + ", ".join(f"{s/n:.3f}" for s in samples[v]))
    RESULTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS}")


if __name__ == "__main__":
    main()
