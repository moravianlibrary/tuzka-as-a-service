# TuzkaOCR engine — memory & OOM investigation

**Date:** 2026-06-12 · **Trigger:** cluster (`dk-taas`) jobs piling up `queued`, AltoEditor batches
failing. **Measured locally** with `tuzkaocr:cpu`, the `test-data/` corpus (100 MZK page scans,
~1500×2200 px, ~10 MB decoded RGB), `OCR_THREADS=1 LINE_WORKERS=1 PAGE_WORKERS=2`.

## Symptom (cluster)
- `taas-ocr-engine-cpu-1` **OOMKilled** (exit 137) at its 4 GiB limit; `cpu-2` sitting at 3.4 GiB.
- After an OOM, the engine's in-flight jobs are lost → its Redis backend inflight counter stays
  high → the submit worker stops dispatching to it → **all load funnels to the other engine**,
  which then fills (`503`) and heads toward OOM too. The reaper eventually frees the stuck counter.
- Downstream: AltoEditor's PERO client polls `400 not processed yet`, then `503 Service busy`, and
  fails the batch. **The client errors are a symptom of the engine OOM/capacity, not a separate bug.**

## Where the memory goes
Model files are **3 MB**; a decoded page is **~10 MB** — neither explains GiB-scale usage. The cost is:
1. The layout detector is a **fully-convolutional segmentation net** run at ~1536 px (`MAX_SIDE`).
   Its intermediate activation maps are hundreds of MB each in fp32 → **~1.3 GiB transient per page**.
2. **ONNX Runtime's CPU memory arena is left ON** (engine never sets `enable_cpu_mem_arena=False`),
   so it grows to the peak working set and **does not return it to the OS** → the high-water mark sticks.
3. **`PAGE_WORKERS=2`** runs two of those at once → ~2.6 GiB resident.

## Memory measurements (peak, MiB; PAGE_WORKERS=2)

| concurrent jobs | arena ON (default) | arena OFF (`enable_cpu_mem_arena=False`) | `kSameAsRequested` config-entry |
|---|---|---|---|
| 1  | 1695 | **910**  | 1711 |
| 2  | 2574 | **1403** | 2571 |
| 4  | 2623 | **1498** | 2658 |
| 8  | 2626 | **1498** | 2660 |
| 16 | 2637 | **1616** | — |
| **idle after load** | **2394** | **264** | (≈ baseline) |

**Findings**
- **The staged buffer is essentially free:** N=2→N=16 (8× more queued jobs) grew peak only ~60 MiB
  → ~**8 MiB per queued job**. `maxInflight`/`MAX_QUEUE` are **not** the OOM cause. *(This corrects the
  initial hypothesis that the 8-deep buffer caused the OOM.)*
- **The lever is `PAGE_WORKERS`** × ~1.3 GiB/page.
- **`enable_cpu_mem_arena=False` cuts peak ~45%** and lets memory **release** (idle 2394 → 264 MiB).
- **`session.arena_extend_strategy=kSameAsRequested` does nothing** on the CPU provider — ORT accepts
  the config string but ignores it (would need a registered shared `OrtArenaCfg` allocator). Dead end.

## Throughput / latency cost of arena-off (local, 24-page batch)

| metric | arena ON | arena OFF | Δ |
|---|---|---|---|
| single-page latency (median) | 1.38 s | 1.86 s | +35% |
| batch throughput (24 pages)  | 0.53 pg/s | 0.50 pg/s | **−6%** |

Throughput barely moves under load (compute-bound); single-page latency rises (per-run alloc).
**For a batch workload (AltoEditor bulk), the ~6% is well worth eliminating the OOM crashes.**

## Sizing formula

```
peak_mem ≈ 0.1 GiB  +  PAGE_WORKERS × ~1.3 GiB  +  MAX_QUEUE × ~8 MiB  +  tmpfs scratch
```
- `PAGE_WORKERS=1` → ~1.2–1.7 GiB → fits a **2 GiB** limit (arena off).
- `PAGE_WORKERS=2` → ~1.5 GiB (arena off) / ~2.6 GiB (arena on).

## Recommendations
1. **Engine (TuzkaOCR ≥ v1.2.0):** set **`TUZKAOCR_CPU_MEM_ARENA=false`** (now a built-in config knob,
   wired to `enable_cpu_mem_arena` on both ONNX sessions). ~45% less memory, releases to OS; perf
   cost is within noise on v1.2.0 (older builds showed ~6% batch / +35% single-page latency).
   Wired into `ocrEnginesDefaults.env` (Helm) and the compose `ocr-engine` env.
2. **Don't keep scratch on tmpfs:** `spool`/`results` as `emptyDir` (disk), not `Medium: Memory` —
   tmpfs counts against the pod memory limit.
3. **Chart defaults (`ocrEnginesDefaults`):** with arena-off, `PAGE_WORKERS=2` fits a **~2 GiB** limit
   (or `PAGE_WORKERS=1` in ~1.2 GiB). Keep `maxInflight == MAX_QUEUE` small (≈3–4); the buffer is cheap.
4. **Scale by engine count**, not by threads/page-workers per engine (1-CPU scale-out model).
5. **Stop-gap on the running cluster:** `PAGE_WORKERS=1` via `kubectl set env` halts the OOM until the
   arena-off image rolls out.
