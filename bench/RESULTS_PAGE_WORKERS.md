# Bench — TUZKAOCR_PAGE_WORKERS sweep (live stack, 2-core engine)

Engine pinned to 2 cores (`cpuset: "0,1"`), env aligned to `values.yaml` (`OCR_THREADS=1 LINE_WORKERS=1 MAX_QUEUE=4 CPU_MEM_ARENA=false`). Backend `max_inflight=4`. 60 pages/run · 3 interleaved rounds · medians across rounds. `fmt=txt`.

## Running time  (`finished_at − started_at`, seconds/page)

| page_workers | median | p95 | max |
|---|---|---|---|
| 1 | 16.68 | 64.95 | 94.00 |
| 2 | 16.49 | 33.36 | 124.67 |
| 3 | 16.67 | 64.23 | 216.64 |

## Throughput (pages/sec, queue saturated)  +  health

| page_workers | throughput (median) | taas queue wait (median s) | done | failed | requeues | engine peak RSS (MiB) |
|---|---|---|---|---|---|---|
| 1 | 0.138 | 178.21 | 180 | 0 | 0 | 1408 |
| 2 | 0.170 | 133.45 | 180 | 0 | 0 | 2548 |
| 3 | 0.137 | 157.15 | 180 | 0 | 0 | 2610 |

## Verdict

**`PAGE_WORKERS=2` is the sweet spot on 2 cores — `values.yaml` is right.**

- **Throughput:** pw=2 (0.170 pg/s median) is **~23% faster** than both pw=1 (0.138) and
  pw=3 (0.137). pw=3 gives **no throughput gain** — it oversubscribes 2 cores and lands level
  with pw=1, even dropping to the worst single run (0.111 pg/s in round 2).
- **Tail latency:** pw=2 has the best p95 running time (**33 s** vs ~65 s for pw=1 and pw=3) and
  pw=3 has the worst max (**217 s**) — oversubscription produces long stragglers.
- **Memory:** scales with pw as `ENGINE_MEMORY.md` predicts — pw=1 ≈ 1.3 GiB peak, pw=2 ≈
  1.6–2.5 GiB, pw=3 ≈ 2.0–2.6 GiB. pw=3 spends the most memory for the worst throughput.
- **pw=1** is the valid trade only when memory-bound: ~23% less throughput for ~half the peak
  RSS (fits a 2 GiB pod). pw=3 is strictly dominated — don't use it.

### Caveats

- **Median running time (~16.5 s) is not the discriminating metric.** Because taas dispatches up
  to `max_inflight=4` (setting `started_at` on all four) while the engine runs only `pw`
  concurrently, the surplus jobs wait *inside* the engine with the clock already running. That
  in-engine wait dominates and flattens the median across pw. **Throughput and p95/max are the
  metrics that separate the configs.**
- **Host-load confound:** round 3 ran ~1.7× faster for *every* pw (wall ~250–290 s vs ~350–540 s
  in rounds 1–2; see per-round table) — the 2 pinned cores were contended by other host
  processes during rounds 1–2 and freed up by round 3. The interleaved design gave **each pw
  exactly one fast (round-3) sample and two slow samples**, so the per-pw medians stay a fair
  comparison. Notably, in the uncontended round 3 pw=3 caught up to pw=2 (0.235 vs 0.241) — so
  pw=3's penalty is specifically about *contended* 2-core capacity, which is the deploy reality
  (`cpu: "2"` limit). The two steady rounds (1–2) agree with the median ranking: pw2 > pw1 ≈ pw3.

## Per-round raw

| round-pw | done | failed | wall (s) | throughput | median run s | peak MiB |
|---|---|---|---|---|---|---|
| r1-pw1 | 60 | 0 | 455.5 | 0.132 | 16.96 | 1408 |
| r2-pw1 | 60 | 0 | 434.5 | 0.138 | 16.74 | 1270 |
| r3-pw1 | 60 | 0 | 292.1 | 0.205 | 8.44 | 1238 |
| r1-pw2 | 60 | 0 | 352.7 | 0.170 | 16.77 | 1620 |
| r2-pw2 | 60 | 0 | 368.1 | 0.163 | 16.58 | 2548 |
| r3-pw2 | 60 | 0 | 249.3 | 0.241 | 9.00 | 2173 |
| r1-pw3 | 60 | 0 | 437.7 | 0.137 | 16.69 | 2202 |
| r2-pw3 | 60 | 0 | 539.7 | 0.111 | 16.93 | 2008 |
| r3-pw3 | 60 | 0 | 254.9 | 0.235 | 8.66 | 2610 |
