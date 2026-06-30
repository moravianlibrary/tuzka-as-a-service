# Bench — TuzkaOCR pod shape × BLAS policy (throughput per core)

`PAGE_WORKERS = cores` (page-parallel), `OCR_THREADS=1 LINE_WORKERS=1 CPU_MEM_ARENA=false`,
cpuset-pinned, backend `max_inflight=8`, engine `MAX_QUEUE=8`. 40 pages/run · 3 interleaved
rounds · medians across rounds. `fmt=txt`.

**Goal: highest pages/sec PER CORE = best pod shape to replicate across a cluster CPU budget.**

> Note: on a **1-core** pod "capped" and "uncapped" are the *same* config (BLAS threads = cores
> = 1), so their six runs are pooled below.

| pod shape | BLAS | total pg/s (median) | **pg/s per core** | peak RSS MiB |
|---|---|---|---|---|
| **1-core / pw=1** | n/a (=1) | 0.143 | **0.143** | ~1.3 GiB |
| 2-core / pw=2 | capped | 0.189 | **0.094** | ~2.0 GiB |
| 2-core / pw=2 | uncapped | 0.170 | **0.085** | ~2.0 GiB |
| 4-core / pw=4 | capped | 0.210 | **0.052** | ~2.0 GiB |
| 4-core / pw=4 | uncapped | 0.205 | **0.051** | ~2.4 GiB |

## Verdict — for max cluster throughput, deploy many **1-core / `PAGE_WORKERS=1`** pods

**The engine scales poorly across cores, so per-core efficiency is highest at 1 core.**
Adding cores gives sharply diminishing returns — 4 cores do only ~1.5× the work of 1 core,
not 4×:

| shape | total pg/s | vs 1-core total | pg/s per core | core efficiency |
|---|---|---|---|---|
| 1-core | 0.143 | 1.0× | 0.143 | 100% |
| 2-core | ~0.18 | 1.3× | ~0.09 | ~63% |
| 4-core | ~0.21 | 1.5× | ~0.05 | ~37% |

So for a **fixed cluster CPU budget**, smaller pods win big. On a 32-core budget:

- **32 × 1-core pods → ~4.6 pg/s**
- 16 × 2-core pods → ~2.9 pg/s
- 8 × 4-core pods → ~1.7 pg/s

**1-core pods deliver ~1.6× the throughput of 2-core and ~2.7× of 4-core for the same CPU.**
This confirms and quantifies `ENGINE_MEMORY.md`'s "scale by engine count, not threads/
page-workers per engine (1-CPU scale-out model)."

### Recommended Kubernetes config (throughput-optimal)

```yaml
ocrEngine:
  enabled: true
  maxInflight: 2                 # small dispatch buffer over pw=1 (keeps the 1 worker fed)
  env:
    TUZKAOCR_PAGE_WORKERS: "1"
    TUZKAOCR_OCR_THREADS:  "1"
    TUZKAOCR_LINE_WORKERS: "1"
    TUZKAOCR_MAX_QUEUE:    "2"
    TUZKAOCR_CPU_MEM_ARENA: "false"
    # REQUIRED in k8s: without these, numpy/opencv/BLAS auto-detect the NODE's full core
    # count and spawn dozens of threads inside a 1-core pod → thrash. Pin them to the pod.
    OMP_NUM_THREADS: "1"
    OPENBLAS_NUM_THREADS: "1"
    MKL_NUM_THREADS: "1"
    NUMEXPR_NUM_THREADS: "1"
    VECLIB_MAXIMUM_THREADS: "1"
  resources:
    requests: { cpu: "1", memory: 1500Mi }
    limits:   { cpu: "1", memory: 2Gi }     # 1-core peak ~1.3 GiB (arena off) fits 2Gi
  autoscaling:
    mode: keda                   # scale replicas on Redis ocr-jobs queue depth
    minReplicas: <baseline>
    maxReplicas: <as many 1-core pods as the cluster CPU allows>
```

The throughput lever is then purely **replica count** — push `maxReplicas` to your CPU budget.

### Notes / nuance

- **BLAS capped vs uncapped is a wash** here (capped marginally ahead at 2/4-core; identical at
  1-core). The real point isn't capped-vs-uncapped, it's that the threads **must be pinned to the
  pod's core budget** — the dangerous default in k8s is *unset*, which auto-detects the whole node.
- **No contradiction with the PAGE_WORKERS bench:** on a *fixed* 2-core pod, pw=2 (0.18) still
  beats pw=1 (0.14) — use both cores you were given. But two 1-core pods (2 × 0.14 = 0.28) beat one
  2-core pod (0.18) for the *same* 2 cores. If you choose the pod size, choose 1 core.
- **Likely cause of poor multi-core scaling:** the engine's page workers are *threads*
  (`ThreadPoolExecutor` in `jobs.py`) sharing one process. Outside the ONNX inference call
  (which releases the GIL), per-page Python work (decode, layout orchestration, post-processing,
  result write) holds the GIL and serializes across workers. A *process*-based page pool would
  likely scale better within a pod — an engine-code change, out of scope here, but the highest-
  leverage future optimization if you want bigger pods to pay off.

## Caveats (data quality)

- **Round 3 was host-load contaminated:** throughput collapsed ~3× for most configs (e.g.
  2c-capped 0.197 → 0.069) and **8 jobs hit reaper-timeout failures**. The **median-of-3** design
  absorbs the single bad round, and critically the **per-core ranking is monotonic and identical
  in both clean rounds (1 and 2)** — 1-core > 2-core > 4-core every time — so the qualitative
  conclusion is robust. Absolute pg/s values carry ±~20% noise; re-run on a quiet host for tighter
  numbers (the ranking won't change).
- Single engine replica, fixed 40-page corpus, `fmt=txt`. `OCR_THREADS` (ONNX intra-op) held at 1
  per the page-parallel premise — intra-op vs page-parallel is a separate axis not explored here.

## Per-round raw

| config | round | done | failed | wall s | pg/s | pg/s/core | peak MiB |
|---|---|---|---|---|---|---|---|
| 1c-capped   | 1 | 40 | 0 | 225.4 | 0.177 | 0.177 | 1296 |
| 1c-capped   | 2 | 40 | 0 | 326.3 | 0.123 | 0.123 | 1232 |
| 1c-capped   | 3 | 40 | 0 | 572.4 | 0.070 | 0.070 | 1332 |
| 1c-uncapped | 1 | 40 | 0 | 247.1 | 0.162 | 0.162 | 1357 |
| 1c-uncapped | 2 | 40 | 0 | 212.2 | 0.189 | 0.189 | 1333 |
| 1c-uncapped | 3 | 36 | 4 | 540.5 | 0.067 | 0.067 | 1524 |
| 2c-capped   | 1 | 40 | 0 | 211.9 | 0.189 | 0.094 | 1961 |
| 2c-capped   | 2 | 40 | 0 | 203.4 | 0.197 | 0.098 | 2114 |
| 2c-capped   | 3 | 36 | 4 | 522.2 | 0.069 | 0.034 | 2054 |
| 2c-uncapped | 1 | 40 | 0 | 235.8 | 0.170 | 0.085 | 2040 |
| 2c-uncapped | 2 | 40 | 0 | 212.0 | 0.189 | 0.094 | 1711 |
| 2c-uncapped | 3 | 40 | 0 | 465.9 | 0.086 | 0.043 | 1715 |
| 4c-capped   | 1 | 40 | 0 | 168.2 | 0.238 | 0.059 | 1981 |
| 4c-capped   | 2 | 40 | 0 | 190.8 | 0.210 | 0.052 | 2007 |
| 4c-capped   | 3 | 40 | 0 | 218.0 | 0.183 | 0.046 | 2091 |
| 4c-uncapped | 1 | 40 | 0 | 138.1 | 0.290 | 0.072 | 2060 |
| 4c-uncapped | 2 | 40 | 0 | 194.7 | 0.205 | 0.051 | 2476 |
| 4c-uncapped | 3 | 40 | 0 | 270.3 | 0.148 | 0.037 | 2417 |
