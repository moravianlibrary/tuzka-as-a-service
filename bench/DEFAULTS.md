# TuzkaOCR — recommended threading defaults

The recognizer runs **one text line at a time** (tiny tensors), so parallelism
belongs at the **line level** (`line_workers`), not in ONNX Runtime's intra-op
thread pool (`ocr_threads`). Setting `ocr_threads=1` and parallelising across
lines is the clean, safe choice on this CPU.

> Measured on the **current static `QLinearConv` recognizer** (~6 ms/line). With
> the older dynamic `ConvInteger` model the gap was far larger (`4/4`≈39 s vs
> `1/8`≈24 s, and `ocr_threads=16` blew up to ~185 s); the fast model shrinks
> those penalties, but the *ordering* is unchanged — line-level parallelism wins.

## Recommended values

| setting | shipped | recommended | why |
|---|---|---|---|
| `ocr_threads` (ONNX intra-op) | 4 | **1** | >1 is slower for this single-line model |
| `line_workers` (parallel lines/page) | 4 | **deployment-dependent** (see below) | the only parallelism that helps |

**`line_workers` by deployment:**
- **Many 1-CPU engines** (scale-out, each pinned to one core): `line_workers=1` — each engine is serial; scale throughput by running more engines.
- **Single multi-core process** (latency on one page): `line_workers ≈ min(8, cores)` — knee of the curve is ~8.

`ocr_threads=1` is correct in **both** cases.

## How to set (no code change needed)

CLI flags:
```bash
# single 1-CPU engine behaviour
tuzkaocr <img> --ocr-threads 1 --line-workers 1
```
or environment (e.g. `tuzkaocr.env`, docker-compose):
```bash
TUZKAOCR_OCR_THREADS=1
TUZKAOCR_LINE_WORKERS=1     # or up to ~8 for single-process multi-core
```

## Measured impact (dense page, ~4860 words, current QLinearConv model)

| ocr_threads | line_workers | time |
|---|---|---|
| 1 | 1 (1-CPU engine) | 16.3 s |
| **1** | **8** | **8.3 s** ← clean best |
| 1 | 16 | 8.5 s |
| 4 | 4 (shipped default) | 8.3 s |
| 8 | 1 | 14.7 s |
| 16 | 1 | 17.7 s |

Reading it:
- **Line-level parallelism wins**: `1×8` (8.3 s) beats `8×1` (14.7 s) — same core
  budget, very different result.
- `ocr_threads=1` is never worse and avoids the high-intra-op penalty
  (`16×1` = 17.7 s). Use it.
- The shipped `4/4` now ties the best (8.3 s) on this model, so it is no longer
  harmful — but `1 / line_workers` is the clearer, safer rule.
- A single 1-CPU engine does a dense page in **~16 s**; scale throughput by
  running more engines, not more threads.
