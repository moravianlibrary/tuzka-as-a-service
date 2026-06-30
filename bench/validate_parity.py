#!/usr/bin/env python
"""Corpus parity + speed gate for the numba path_penalty kernel (TuzkaOCR-perf).

Clustering (the only place the numba kernel runs) is independent of OCR
recognition, so this validates at the layout level: run the layout model once
per image to get the feature maps, then run maps_to_regions twice — once with
the numba penalty (production) and once with the original OpenCV penalty
(_path_penalty_cv2) — and compare the resulting region partition exactly. No
slow OCR pass, so the full 100-image corpus runs in a few minutes.

Usage:
    <perf-venv>/bin/python bench/validate_parity.py [image_dir] [--perf-repo DIR] [--limit N]

Exit code is non-zero if any image's region partition differs (CI gate).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PERF = HERE.parent / "TuzkaOCR-perf"
DEFAULT_IMAGES = HERE.parent / "test-data"
DS_LEVELS = (3, 2, 1)


def _partition(regions):
    # canonical signature of the line grouping: a set of frozensets of line keys
    parts = []
    for r in regions:
        parts.append(frozenset(tuple(ln.baseline) for ln in r.lines))
    return frozenset(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="?", default=str(DEFAULT_IMAGES))
    ap.add_argument("--perf-repo", default=str(DEFAULT_PERF))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    # make the perf repo's tuzkaocr importable (ahead of any installed copy)
    sys.path.insert(0, str(Path(args.perf_repo).resolve()))
    import cv2
    import numpy as np
    import tuzkaocr.layout.postprocess as pp
    from tuzkaocr.layout.detector import LayoutDetector
    from tuzkaocr.layout.postprocess import maps_to_regions
    from tuzkaocr._models import resolve

    root = Path(args.images)
    images = ([root] if root.is_file()
              else sorted(root.glob("*_image.jpeg")))
    if args.limit:
        images = images[: args.limit]
    if not images:
        print("no images found", file=sys.stderr)
        return 2

    det = LayoutDetector(str(resolve("dec-A-v4.onnx")), device="cpu", threads=1)
    nb, cv = pp._path_penalty, pp._path_penalty_cv2
    diffs = []
    flips = 0
    t_nb = t_cv = 0.0
    n_calls = 0
    mism = 0

    for img_path in images:
        img = cv2.imread(str(img_path))
        if img is None:
            print(f"[SKIP] {img_path.name}: unreadable")
            continue
        img_mismatch = False
        for ds in DS_LEVELS:
            maps, _ = det.get_maps(img, ds)

            calls = {"d": [], "f": 0, "tn": 0.0, "tc": 0.0}

            def probe(*a, **k):
                t0 = time.perf_counter(); r_nb = nb(*a, **k); calls["tn"] += time.perf_counter() - t0
                t1 = time.perf_counter(); r_cv = cv(*a, **k); calls["tc"] += time.perf_counter() - t1
                calls["d"].append(abs(r_nb - r_cv))
                if (r_nb < 0.15) != (r_cv < 0.15):
                    calls["f"] += 1
                return r_nb

            pp._path_penalty = nb
            part_nb = _partition(maps_to_regions(maps))
            pp._path_penalty = cv
            part_cv = _partition(maps_to_regions(maps))
            # re-run once more only to collect per-call deltas/timing
            pp._path_penalty = probe
            maps_to_regions(maps)
            pp._path_penalty = nb

            diffs.extend(calls["d"]); flips += calls["f"]; n_calls += len(calls["d"])
            t_nb += calls["tn"]; t_cv += calls["tc"]
            if part_nb != part_cv:
                img_mismatch = True
        mism += 1 if img_mismatch else 0
        print(f"[{'DIFF' if img_mismatch else 'OK '}] {img_path.name}")

    d = np.array(diffs) if diffs else np.zeros(1)
    print("-" * 64)
    print(f"images: {len(images)}   region-partition mismatches: {mism}")
    print(f"penalty calls: {n_calls}   |delta| max={d.max():.2e} mean={d.mean():.2e}")
    print(f"borderline union flips @0.15: {flips} ({100*flips/max(1,n_calls):.3f}%)")
    if t_nb:
        print(f"path_penalty time: numba={t_nb*1000:.0f}ms cv2={t_cv*1000:.0f}ms  speedup={t_cv/t_nb:.1f}x")
    return 1 if mism else 0


if __name__ == "__main__":
    raise SystemExit(main())
