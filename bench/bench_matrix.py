#!/usr/bin/env python
"""Branch × thread-scenario matrix bench (1 model load per 100-doc batch).

For each git variant (base + one branch per optimization) and each thread
scenario, runs the whole corpus in a single batch process (--workers 1, so the
model loads once) and records s/page. Also checks word-content parity vs base.

Variants are git branches in TuzkaOCR-perf; the driver checks each one out.
Thread scenarios vary line_workers with ocr_threads pinned to 1.

Usage: python bench/bench_matrix.py [--n 100] [--lws 1,2,4,8,16]
Writes bench/RESULTS_MATRIX.md and streams progress to stdout.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path

PARENT = Path(__file__).resolve().parent.parent
PERF = PARENT / "TuzkaOCR-perf"
VENV = PARENT / "TuzkaOCR" / ".venv" / "bin" / "python"
IMAGES = PARENT / "test-data"
OUT = PARENT / "bench" / "matrix_out"
RESULTS = PARENT / "bench" / "RESULTS_MATRIX.md"

VARIANTS = ["main", "bench-ctc", "bench-median", "bench-numba", "bench-small-numpy"]
LABEL = {"main": "base", "bench-ctc": "ctc", "bench-median": "median",
         "bench-numba": "numba", "bench-small-numpy": "small-numpy"}
PARITY_LW = 8  # word-content parity is threading-independent; compare at one lw


def git_checkout(branch: str) -> None:
    subprocess.run(["git", "-C", str(PERF), "checkout", branch],
                   check=True, capture_output=True, text=True)


def run_batch(variant: str, lw: int, n: int) -> tuple[float, Path]:
    out_dir = OUT / f"{variant}_lw{lw}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # process exactly the first n images by passing a temp dir? cli batches a dir,
    # so we bench the whole test-data dir (n is informational unless ==100).
    cmd = [str(VENV), "cli.py", str(IMAGES), "--batch", "--out-dir", str(out_dir),
           "--workers", "1", "--ocr-threads", "1", "--line-workers", str(lw),
           "--format", "alto"]
    t = time.perf_counter()
    subprocess.run(cmd, cwd=str(PERF), capture_output=True, text=True, check=True)
    return time.perf_counter() - t, out_dir


def words(path: Path) -> list[str]:
    return sorted(re.findall(r'CONTENT="([^"]*)"', path.read_text(encoding="utf-8")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--lws", default="1,2,4,8,16")
    args = ap.parse_args()
    lws = [int(x) for x in args.lws.split(",")]
    n = len(sorted(IMAGES.glob("*_image.jpeg"))[: args.n])

    timings: dict[tuple[str, int], float] = {}
    try:
        for v in VARIANTS:
            git_checkout(v)
            for lw in lws:
                dt, _ = run_batch(v, lw, args.n)
                timings[(v, lw)] = dt
                print(f"[{LABEL[v]:<11} lw={lw:<2}] {dt:7.1f}s total  {dt / n:5.2f}s/page",
                      flush=True)
    finally:
        git_checkout("main")

    # parity vs base at PARITY_LW
    parity = {}
    base_dir = OUT / f"main_lw{PARITY_LW}"
    for v in VARIANTS:
        if v == "main":
            continue
        vd = OUT / f"{v}_lw{PARITY_LW}"
        mism = 0
        for img in sorted(IMAGES.glob("*_image.jpeg"))[: args.n]:
            b = base_dir / f"{img.stem}.alto.xml"
            p = vd / f"{img.stem}.alto.xml"
            if b.exists() and p.exists() and words(b) != words(p):
                mism += 1
        parity[v] = mism

    # write markdown
    lines = ["# Matrix bench — branch × thread scenario (100 docs, 1 model load)\n"]
    lines.append(f"Corpus: {n} pages · `--workers 1` (one model load) · `ocr_threads=1` · static QLinearConv model\n")
    lines.append("## Seconds per page\n")
    header = "| variant | " + " | ".join(f"lw={lw}" for lw in lws) + " | text-diff vs base |"
    lines.append(header)
    lines.append("|" + "---|" * (len(lws) + 2))
    for v in VARIANTS:
        row = [LABEL[v]]
        for lw in lws:
            row.append(f"{timings[(v, lw)] / n:.2f}s")
        row.append("—" if v == "main" else f"{parity[v]}/{n}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("\n## Total batch seconds\n")
    lines.append(header.replace(" text-diff vs base ", ""))
    lines.append("|" + "---|" * (len(lws) + 1))
    for v in VARIANTS:
        row = [LABEL[v]] + [f"{timings[(v, lw)]:.0f}s" for lw in lws]
        lines.append("| " + " | ".join(row) + " |")
    RESULTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS}")


if __name__ == "__main__":
    main()
