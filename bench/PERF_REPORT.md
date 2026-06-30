# TuzkaOCR — Performance Investigation & Optimization Report

**Date:** 2026-06-10
**Host:** Intel Core i9-14900HX — 24 physical cores (8 P + 16 E), 32 threads. AVX2 + **AVX-VNNI**, **no AVX-512**.
**Runtime:** onnxruntime 1.25.1 (CPUExecutionProvider)
**Test page (single):** `5058bdf8-…_image.jpeg`, 1500×2121, dense (4880 words / 834 lines / 10 regions)
**Corpus:** `test-data/*_image.jpeg` (100 pages)

---

## TL;DR

The pipeline was slow for three independent reasons, in order of impact:

1. **int8 recognizer uses the wrong quantization scheme** — `ConvInteger` (dynamic) instead of `QLinearConv` (static). On this CPU int8 as-shipped is **3.4× *slower* than fp32**; done right it would be **4× faster than fp32**. → ~6–7× recognizer win available. *(Needs the fp32 source model + calibration data — handed to model team.)*
2. **ONNX intra-op threading hurts** this single-line-at-a-time model. Default `ocr_threads=4` is slower than `1`. → ~1.6× win, applied as new defaults.
3. **Layout post-processing** spends seconds in `np.median`/`np.percentile`/`cv2.line` called tens of thousands of times. → vectorized (numpy) + a numba kernel, ~3–4× on the Python side.

---

## 1. The int8 recognizer (biggest single lever)

The recognizer is a CNN (15 int8 convs + GRU head). Op-level profiling of `rec-E-v5.int8.onnx` (single-thread, realistic line crop):

| op | % of inference | note |
|---|---|---|
| `ConvInteger` | **91.5%** | the int8 convolutions |
| `DynamicQuantizeLinear` | 1.5% | per-inference activation scaling — *not* the bottleneck |
| GRU / others | ~7% | |

Micro-benchmark of the dominant conv shape `(512,256,3)` on this CPU:

| implementation | time/op | vs fp32 |
|---|---|---|
| fp32 `Conv` | 1.12 ms | 1.0× |
| **int8 `ConvInteger`** (dynamic — *what ships*) | **3.87 ms** | **0.29× (3.4× slower)** |
| int8 `QLinearConv` (static) | 0.28 ms | **4.0× faster** |

**Root cause:** the model was quantized with `quantize_dynamic`, which emits `ConvInteger` — and ONNX Runtime has **no optimized `ConvInteger` kernel**, so it falls back to a slow reference path. ORT guidance: **static quantization for CNNs**, dynamic only for RNN/Transformer.

**Fix (model team):** re-quantize the fp32 recognizer with `quantize_static` (QDQ format, a few hundred line-crops for calibration) → `QLinearConv`, ~14× faster than today's convs. Quick fallback: ship fp32 (3.4× faster than current on this CPU, ~4× bigger model).

---

## 2. Threading

The recognizer runs **one text line at a time** (tiny tensors); ONNX intra-op threads only add barrier/spin-wait overhead, made worse by the hybrid P/E-core scheduler.

Clean single-page measurements:

| config | time |
|---|---|
| default `ocr_threads=4, line_workers=4` | 39.2 s |
| **`ocr_threads=1, line_workers=8`** | **23.8 s** |
| `ocr_threads=16` (naive "use all cores") | 185 s |

`line_workers` sweep at `ocr_threads=1`: 4→31.9s, 8→27.0s, 16→23.5s, 24→22.7s (knee ~8).

**Applied as new defaults** (both repos): `ocr_threads=1`, `line_workers=min(8, cpu_count)` (affinity-aware for Docker). CLI flags and `TUZKAOCR_*` env still override.

---

## 3. Adaptive downsampling

`_run` re-runs full detect+OCR per downsample level (DS 3→2→1) until quality passes. Dense pages escalate through all three: DS=3 alone finds only 248/4880 words (→3.8 s) but the full adaptive run is 23.8 s. Correct behaviour, but it multiplies the per-pass cost — which is why the post-processing wins below matter once int8 is fixed.

---

## 4. Post-processing hotspots (Python side)

`line_profiler`, full adaptive run (totals already include the 3 passes):

| function | time | cause |
|---|---|---|
| `_baseline_points_from_xy` | 2.09 s | `np.median` on tiny segments × 66k |
| `_path_penalty` (via `_cluster_regions`, O(n²)) | 1.93 s | `cv2.line` rasterization × 22k |
| `_greedy_ctc` | 0.89 s | full softmax + Python timestep loop (447k iters) |
| `_sample_heights_from_xy` | 0.50 s | `np.percentile` × 5k |
| `_baseline_to_polygon_normal` | 0.45 s | scattered small numpy |

### Optimizations applied (in `TuzkaOCR-perf`)

| change | method | measured | output |
|---|---|---|---|
| `_greedy_ctc` | vectorized collapse + pmax (numpy) | 65→38 µs/line (1.7×) | **byte-identical** |
| `_baseline_points_from_xy` | lexsort grouped median (numpy) | 2044→248 ms (**8.2×**) | **byte-identical** |
| `_path_penalty` | **numba** kernel | 15469→608 ms corpus (**25.4×**) | text-identical; see §6 |

---

## 5. numba vs numpy — the verdict

numba only beats *vectorized numpy* where numpy structurally can't vectorize (irregular/sequential loops):

| workload | numpy | numba | numba's marginal value |
|---|---|---|---|
| grouped median | 8.2× | 13× | small — numpy already wins |
| CTC collapse | 1.7× | ~same | none |
| **`_path_penalty` (union-find + per-pair rasterization)** | **~1× (can't)** | **25×** | **large — only numba helps** |

→ Used **numpy for the vectorizable kernels** (no dependency) and **numba only for `_path_penalty`** (the one place it earns the dependency). Marginal numba gains elsewhere don't justify the ~60 MB `numba`/`llvmlite` weight or the per-process JIT/import cost.

---

## 6. Parity (100-page corpus)

The numba kernel approximates `cv2.line`'s thick rasterization (cv2 paints a ~5px tapered band; IoU of the simple stamp vs cv2 ≈ 0.61).

| metric | result |
|---|---|
| **Text / words / lines** | **identical on all 100 pages** |
| TextBlock grouping | differs on **44/100 pages** |
| penalty calls | 50,943 |
| borderline union-flips @0.15 threshold | 313 (**0.614%**) |
| penalty \|Δ\| | max 0.113, mean 0.0024 |
| path_penalty speedup | **25.4×** |

`_cluster_regions` only *groups* (penalty-independent) lines, so no OCR text changes — only which `TextBlock` owns a borderline line, and reading order. **Decision pending:** accept as-is (text-faithful) / exact-parity rewrite (rasterize each line once with real `cv2.line` + column prefix-sums) / revert numba and keep numpy-only (fully block-identical, loses ~1.9 s/page).

Run the gate yourself: `python bench/validate_parity.py [--limit N]`

---

## 7. CPU-count sweep (end-to-end A/B)

`taskset` to P cores, `ocr_threads=1`, `line_workers=P`, baseline vs perf, mean s/page.

> ⚠️ **Caveats:** end-to-end is OCR-bound by the unfixed int8 `ConvInteger`, so post-processing gains are masked. The sampled pages here are light (≈4 s), where each CLI process pays numba's ~0.5–1 s import tax with little clustering to amortize it — so **perf shows ~0.9× on light CLI pages**. On dense pages and in the long-running API service (numba imported once) the kernel is a net win. Numbers below are a small sample; regenerate with `python bench/bench_threads.py --n 6`.

_(6-page sample, light pages ≈4 s each)_

| cpus | baseline | perf | speedup |
|---|---|---|---|
| 1 | 4.27 s | 4.68 s | 0.91× |
| 2 | 4.64 s | 4.88 s | 0.95× |
| 4 | 2.92 s | 3.16 s | 0.92× |
| 8 | 2.20 s | 2.41 s | 0.91× |
| 16 | 1.76 s | 2.04 s | 0.86× |

Scaling with cores is real (4.4→1.7 s, 1→16 cpu). The perf-vs-baseline gap is the numba import tax on light single-image CLI runs — **not representative of dense pages or the API service**, and it vanishes entirely once the int8 fix removes the OCR ceiling.

---

## 8. Files changed

**`TuzkaOCR-perf` @ branch `perf/postprocess-vectorize-numba`** (not yet committed):
- `tuzkaocr/config.py`, `cli.py`, `tuzkaocr.env` — threading defaults
- `tuzkaocr/ocr/recognizer.py` — CTC vectorization
- `tuzkaocr/layout/postprocess.py` — lexsort median; `_path_penalty` → numba (cv2 version kept as `_path_penalty_cv2`)
- `tuzkaocr/layout/_kernels.py` — new numba kernel
- `pyproject.toml` — `numba==0.65.1`

**Parent `bench/`:** `validate_parity.py`, `bench_threads.py`, this report.
**Shared venv:** `TuzkaOCR/.venv` (has numba); both repos run via `cd <repo> && <venv>/bin/python …` (cwd-shadowing loads local code).

## 9. Recommended next steps (priority order)

1. **Static-requantize the recognizer** (`QLinearConv`) — the ~6–7× win. Owns the result; needs fp32 model + calibration crops.
2. **Land the threading defaults** — free 1.6×, output-identical.
3. **Decide the numba parity question** (§6), then land numpy + numba post-processing wins — these dominate runtime *after* step 1.
