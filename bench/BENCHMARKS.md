# TuzkaOCR — Benchmark Results & Optimization Report (consolidated)

**Date:** 2026-06-10
**Host:** Intel Core i9-14900HX — 24 physical cores (8 P + 16 E), 32 threads. AVX2 + **AVX-VNNI**, **no AVX-512**. (laptop — thermal throttling matters for benchmarking)
**Runtime:** onnxruntime 1.25.1, CPUExecutionProvider
**Corpus:** `test-data/*_image.jpeg` — 100 scanned pages. Dense reference page: 1500×2121, ~4860 words / 834 lines.

> **TL;DR** — The decisive win was re-quantizing the recognizer from dynamic
> `ConvInteger` to static `QLinearConv` (~12× faster OCR; page ~24 s → ~1.5–2 s).
> After that, the page is OCR/model-bound and every post-processing optimization
> is **sub-noise end-to-end**. Threading: `ocr_threads=1` + line-level parallelism.

---

## 1. int8 recognizer — the decisive lever

### 1a. Single-conv micro-benchmark (dominant shape `(512,256,3)`, 1 thread)

| implementation | time/op | vs fp32 |
|---|---|---|
| fp32 `Conv` | 1.12 ms | 1.0× |
| int8 `ConvInteger` (dynamic — original model) | 3.87 ms | **0.29× (3.4× slower)** |
| int8 `QLinearConv` (static — current model) | 0.28 ms | **4.0× faster** |

`ConvInteger` (emitted by `quantize_dynamic`) has no optimized ORT kernel → slower
than fp32. `QLinearConv` (static `quantize_static`) uses the fast VNNI path.
**~14× between the two.** ORT guidance: static quant for CNNs, dynamic for RNN/Transformer.

### 1b. Op-level profile of the original dynamic model

`ConvInteger` = **91.5%** of inference; `DynamicQuantizeLinear` = 1.5% (quant
overhead was *not* the bottleneck — the unoptimized integer-conv kernel was).

### 1c. Realized recognizer speedup (300 lines, 1 thread)

| recognizer | ms/line |
|---|---|
| original dynamic `ConvInteger` | ~74 |
| current static `QLinearConv` (committed on main, `eb8b55a`) | ~6 |

≈ **12× faster OCR**, on this CPU, from the requant alone. No code change needed in
TuzkaOCR — just the model file (`rec-E-v5.int8.onnx`, `rec-E-v4k7.int8.onnx`).

---

## 2. Threading defaults

The recognizer runs **one text line at a time** (tiny tensors), so parallelism
belongs at the **line level** (`line_workers`), not in ONNX Runtime's intra-op
thread pool (`ocr_threads`). `ocr_threads=1` is the clean, safe choice.

### 2a. Sweep (current QLinearConv model, dense page ~4860 words)

| ocr_threads | line_workers | time | note |
|---|---|---|---|
| 1 | 1 | 16.3 s | 1-CPU engine |
| **1** | **8** | **8.3 s** | clean best |
| 1 | 16 | 8.5 s | |
| 4 | 4 (shipped default) | 8.3 s | now competitive |
| 8 | 1 | 14.7 s | |
| 16 | 1 | 17.7 s | over-threading hurts |

Line-level parallelism wins (`1×8` beats `8×1`). On the **old** `ConvInteger` model
the spread was far larger: `4/4`=39.2 s, `1/8`=23.8 s, `8/1`=80.7 s, `16/1`=**185 s**
— the fast model shrinks the penalties, but the ordering is unchanged.

### 2b. Recommended values

| setting | shipped | recommended | why |
|---|---|---|---|
| `ocr_threads` (ONNX intra-op) | 4 | **1** | >1 is slower for this single-line model |
| `line_workers` (parallel lines/page) | 4 | **deployment-dependent** | the only parallelism that helps |

**`line_workers` by deployment:**
- **Many 1-CPU engines** (scale-out, each pinned to one core): `line_workers=1`;
  scale throughput by running more engines.
- **Single multi-core process** (latency on one page): `line_workers ≈ min(8, cores)`
  (knee of the curve is ~8).

`ocr_threads=1` is correct in **both** cases.

### 2c. How to set (no code change needed)

CLI flags:
```bash
tuzkaocr <img> --ocr-threads 1 --line-workers 1     # 1-CPU engine
```
or environment (`tuzkaocr.env`, docker-compose):
```bash
TUZKAOCR_OCR_THREADS=1
TUZKAOCR_LINE_WORKERS=1      # or up to ~8 for single-process multi-core
```

---

## 3. Post-processing profile (line_profiler, dense page, per full adaptive run)

| function | time | cause |
|---|---|---|
| `_baseline_points_from_xy` | 2.09 s | `np.median` on tiny segments × 66k |
| `_path_penalty` (via O(n²) `_cluster_regions`) | 1.93 s | `cv2.line` rasterization × 22k |
| `_greedy_ctc` | 0.89 s | full softmax + Python timestep loop (447k iters) |
| `_sample_heights_from_xy` | 0.50 s | `np.percentile` × 5k |
| `_baseline_to_polygon_normal` | 0.45 s | scattered small numpy |

Note: `_path_penalty` is also reached from the adaptive loop, which re-runs full
detect+OCR per downsample level (DS 3→2→1) until quality passes — multiplying the
above on dense pages.

---

## 4. Per-optimization micro-benchmarks (in isolation)

| optimization | method | isolated speedup | parity |
|---|---|---|---|
| CTC decode | vectorized collapse + pmax (numpy) | 65→38 µs/line (**1.7×**) | byte-identical |
| grouped median | lexsort + middle-index (numpy) | 2044→248 ms (**8.2×**) | byte-identical |
| grouped median | numba njit (rejected) | 2044→162 ms (13×) | identical, but +dependency |
| `_path_penalty` | numba kernel | 1617→152 ms/page in-proc (**10.7×**); corpus 15.5→0.6 s (**25.4×**) | ~6–8/100 pages differ |
| percentile + polygon | combined call + vectorized round | small (~0.4–0.6 s/dense page) | byte-identical |

numba beats numpy **only** where numpy can't vectorize (the union-find + per-pair
rasterization in clustering); elsewhere numpy already captures the win, so numba's
~60 MB dependency + idle 32-thread pool isn't justified.

---

## 5. Code changes — from what to what

### 5.1 `bench-ctc` — `tuzkaocr/ocr/recognizer.py` :: `_greedy_ctc`

```python
# BEFORE
    m = logits.max(axis=-1, keepdims=True)
    probs = np.exp(logits - m)
    probs /= probs.sum(axis=-1, keepdims=True)
    pmax = probs.max(axis=-1)

    char_events: List[Tuple[str, int]] = []
    confs: List[float] = []
    prev = 0
    for t, idx in enumerate(best):
        if idx != 0 and idx != prev:
            char_events.append((chars[idx - 1], t))
            confs.append(float(pmax[t]))
        prev = idx
```
```python
# AFTER  (pmax[t] == softmax(logits[t]).max() == 1/Σexp(logits[t]-max); numerically identical)
    m = logits.max(axis=-1)
    pmax = 1.0 / np.exp(logits - m[:, None]).sum(axis=-1)

    keep = best != 0
    keep[1:] &= best[1:] != best[:-1]
    ts = np.flatnonzero(keep)
    char_events: List[Tuple[str, int]] = [(chars[best[t] - 1], int(t)) for t in ts.tolist()]
    confs: List[float] = pmax[ts].tolist()
```

### 5.2 `bench-median` — `tuzkaocr/layout/postprocess.py` :: `_baseline_points_from_xy`

```python
# BEFORE  (per-x-column Python loop calling np.median on tiny segments)
    order = np.argsort(xs, kind="stable")
    xs_s = xs[order]; ys_s = ys[order]
    starts = np.concatenate(([0], np.flatnonzero(np.diff(xs_s)) + 1))
    ends = np.concatenate((starts[1:], [xs_s.size]))
    uniq_x = xs_s[starts]
    targets = np.arange(x_min, x_max + 1, step, dtype=xs.dtype)
    idx = np.searchsorted(uniq_x, targets)
    safe_idx = np.clip(idx, 0, uniq_x.size - 1)
    hit = (idx < uniq_x.size) & (uniq_x[safe_idx] == targets)
    pts: List[Tuple[int, int]] = []
    for t, h, i in zip(targets.tolist(), hit.tolist(), idx.tolist()):
        if not h:
            continue
        seg = ys_s[starts[i]:ends[i]]
        pts.append((int(t), int(np.median(seg))))
    if pts and pts[-1][0] != x_max:
        i = uniq_x.size - 1
        seg = ys_s[starts[i]:ends[i]]
        pts.append((int(x_max), int(np.median(seg))))
    return pts
```
```python
# AFTER  (lexsort (x,y) -> y sorted within each x-group -> median is a middle-index)
    order = np.lexsort((ys, xs))
    xs_s = xs[order]; ys_s = ys[order]
    starts = np.concatenate(([0], np.flatnonzero(np.diff(xs_s)) + 1))
    ends = np.concatenate((starts[1:], [xs_s.size]))
    uniq_x = xs_s[starts]
    sizes = ends - starts
    lo = starts + (sizes - 1) // 2
    hi = starts + sizes // 2
    med = (ys_s[lo].astype(np.int64) + ys_s[hi].astype(np.int64)) // 2   # == int(np.median(seg)) for int y>=0
    targets = np.arange(x_min, x_max + 1, step, dtype=xs.dtype)
    idx = np.searchsorted(uniq_x, targets)
    safe_idx = np.clip(idx, 0, uniq_x.size - 1)
    hit = (idx < uniq_x.size) & (uniq_x[safe_idx] == targets)
    tg = targets[hit].tolist(); mg = med[idx[hit]].tolist()
    pts: List[Tuple[int, int]] = [(int(t), int(m)) for t, m in zip(tg, mg)]
    if pts and pts[-1][0] != x_max:
        pts.append((int(x_max), int(med[uniq_x.size - 1])))
    return pts
```

### 5.3 `bench-small-numpy` — `tuzkaocr/layout/postprocess.py`

`_sample_heights_from_xy` (two percentile calls → one):
```python
# BEFORE
    return (float(np.percentile(ascs, percentile)), float(np.percentile(descs, percentile)))
# AFTER  (same result, half the per-call overhead; asc/desc are the same length)
    pcts = np.percentile(np.stack((ascs, descs)), percentile, axis=1)
    return (float(pcts[0]), float(pcts[1]))
```
`_baseline_to_polygon_normal` (vectorize the rounding loop):
```python
# BEFORE
    return [(int(round(x)), int(round(y))) for x, y in poly]
# AFTER  (np.round is banker's rounding, same as int(round()))
    rounded = np.round(poly).astype(np.int64)
    return [(int(x), int(y)) for x, y in rounded]
```

### 5.4 `bench-numba` — `tuzkaocr/layout/postprocess.py` + new `_kernels.py` + `pyproject.toml`

`postprocess.py` — add import and route `_path_penalty` to the kernel, keeping the
original OpenCV version as `_path_penalty_cv2`:
```python
# added near the top
from . import _kernels

# BEFORE: def _path_penalty(...): <full OpenCV implementation>
# AFTER:
def _path_penalty(b_top, b_bot, shift_top, shift_bot, x1, x2, region_map):
    return _kernels.path_penalty_nb(
        np.ascontiguousarray(b_top, dtype=np.int64),
        np.ascontiguousarray(b_bot, dtype=np.int64),
        int(round(shift_top)), int(round(shift_bot)),
        int(x1), int(x2),
        np.ascontiguousarray(region_map, dtype=np.float32),
    )

def _path_penalty_cv2(...):   # <- the original implementation, unchanged, kept for parity tests
    ...
```
`tuzkaocr/layout/_kernels.py` (new) — numba njit that shifts/clips the two
baselines, rasterizes a 3px thick mask (Bresenham + 3×3 stamp ≈ `cv2.line`), and
averages `region_map` under the masked pixels in `[x1, x2)`. `pyproject.toml` adds
`numba==0.65.1`.

**Caveat:** the 3px stamp approximates `cv2.line`'s ~5px tapered band (IoU ≈ 0.61),
flipping ~0.6% of borderline penalties — see §6/§7 parity.

---

## 6. Parity across the 100-page corpus

| variant | text diffs vs base | block-grouping diffs |
|---|---|---|
| `ctc` | **0/100** | 0/100 |
| `median` | **0/100** | 0/100 |
| `small-numpy` | **0/100** | 0/100 |
| `numba` | **~6–8/100** | ~44/100 |

`_cluster_regions` only *groups* (penalty-independent) lines, so numba changes no
text directly — but its rasterization approximation flips ~0.6% of borderline
penalties, which shifts block membership on ~44 pages and, via adaptive DS-level
selection, OCR text on ~6–8 pages.

---

## 7. End-to-end matrix — branch × thread scenario (100 docs, 1 model load)

`--workers 1` (one model load), `ocr_threads=1`. **s/page.**

| variant | lw=1 | lw=2 | lw=4 | lw=8 | lw=16 | text-diff |
|---|---|---|---|---|---|---|
| base | 1.74 | 1.60 | 1.51 | 1.51 | 1.50 | — |
| ctc | 2.06 | 1.77 | 1.59 | 1.56 | 1.53 | 0/100 |
| median | 2.05 | 1.68 | 1.56 | 1.53 | 1.49 | 0/100 |
| numba | 2.00 | 1.70 | 1.58 | 1.52 | 1.49 | 8/100 |
| small-numpy | 2.05 | 1.67 | 1.56 | 1.50 | 1.48 | 0/100 |

⚠️ **Confounded.** Variants ran sequentially (base first on a cool CPU). The tell:
byte-identical numpy branches show a uniform ~0.3 s/page "penalty" at `lw=1` that
*cannot* come from identical-output code — it is thermal drift + run order. It
shrinks to noise at `lw=16`. Use §8 for the trustworthy cross-variant numbers.

---

## 8. Clean variant bench (confound-controlled) — the trustworthy one

`ocr_threads=1, line_workers=1` (1-CPU engine). Variants **interleaved with
rotating order**, **median of 5 rounds** → kills thermal/order bias.

| variant | median s/page | min | max | vs base |
|---|---|---|---|---|
| base | 1.967 | 1.765 | 2.022 | — |
| **median** | 1.935 | 1.922 | 2.011 | −1.6% |
| small-numpy | 1.954 | 1.900 | 1.997 | −0.6% |
| ctc | 2.023 | 1.877 | 2.080 | +2.8% |
| numba | 2.025 | 2.003 | 2.032 | +2.9% |

Raw per-round s/page:
- base: 1.765, 1.967, 1.999, 1.962, 2.022
- ctc: 1.877, 2.067, 1.977, 2.023, 2.080
- median: 1.935, 1.996, 1.923, 1.922, 2.011
- numba: 2.019, 2.032, 2.003, 2.028, 2.025
- small-numpy: 1.900, 1.997, 1.954, 1.921, 1.973

**All variants within ±3% of base — inside the per-variant spread (±5–14%).** At
`lw=1` the page is OCR/model-bound (~1.97 s/page) and the post-processing opts are
**not end-to-end distinguishable**. (A `±2.8%` delta ≈ 0.055 s/page is *larger than
the entire CTC step*, so such deltas are noise, not code.) The only delta with a
real mechanism is `numba +2.9%` (per-process import + idle 32-thread pool),
consistent with §4–6, and it carries the parity cost.

---

## 9. Conclusions & recommendations

1. **int8 static requant = the win** (≈12× OCR, page ~24 s → ~1.5–2 s). Committed
   on main (`eb8b55a`).
2. **Threading:** `ocr_threads=1`; parallelize across lines or scale by engine
   count (§2).
3. **Post-processing opts are sub-noise end-to-end** post-requant. The numpy ones
   (`ctc`, `median`, `small-numpy`) are byte-identical (0/100) — harmless to ship,
   no real gain at the page level now.
4. **Drop numba** — small net regression (+2.9%) from its import/thread overhead
   *and* an ~6–8/100 text-parity cost, for a clustering speedup that's invisible
   once OCR is fast.
5. **Bigger future levers** (untouched): batch line OCR (one `session.run` per
   width-bucket instead of per line) and skipping redundant adaptive OCR passes —
   both attack the now-dominant OCR cost without numba.

---

## 10. Methodology notes

- This is a thermally-throttling laptop; **sequential A/B is unreliable** (later /
  hotter runs look slower). Always interleave + take medians, or measure in-process.
  §7 vs §8 is the cautionary example.
- Branches (`TuzkaOCR-perf`, local, not pushed): `bench-ctc`, `bench-median`,
  `bench-numba`, `bench-small-numpy` (each isolated + `CHANGES.md`). Base = `main`.
- Scripts (`bench/`): `bench_matrix.py`, `bench_matrix_clean.py`, `bench_batch.py`,
  `bench_threads.py`, `validate_parity.py`. Shared venv: `TuzkaOCR/.venv`.
