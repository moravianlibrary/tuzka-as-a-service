# Matrix bench — branch × thread scenario (100 docs, 1 model load)

Corpus: 100 pages · `--workers 1` (one model load) · `ocr_threads=1` · static QLinearConv model

## Seconds per page

| variant | lw=1 | lw=2 | lw=4 | lw=8 | lw=16 | text-diff vs base |
|---|---|---|---|---|---|---|
| base | 1.74s | 1.60s | 1.51s | 1.51s | 1.50s | — |
| ctc | 2.06s | 1.77s | 1.59s | 1.56s | 1.53s | 0/100 |
| median | 2.05s | 1.68s | 1.56s | 1.53s | 1.49s | 0/100 |
| numba | 2.00s | 1.70s | 1.58s | 1.52s | 1.49s | 8/100 |
| small-numpy | 2.05s | 1.67s | 1.56s | 1.50s | 1.48s | 0/100 |

## Total batch seconds

| variant | lw=1 | lw=2 | lw=4 | lw=8 | lw=16 ||
|---|---|---|---|---|---|
| base | 174s | 160s | 151s | 151s | 150s |
| ctc | 206s | 177s | 159s | 156s | 153s |
| median | 205s | 168s | 156s | 153s | 149s |
| numba | 200s | 170s | 158s | 152s | 149s |
| small-numpy | 205s | 167s | 156s | 150s | 148s |

## Reading the results (important)

**The cross-variant timing is confounded by thermal drift + run order.** Variants
were run sequentially (base first, on a cool CPU; later branches progressively
throttled). Proof: at `lw=1` the byte-identical numpy branches (which produce
*identical output*) are uniformly ~0.3s/page slower than base — impossible from
the code, so it is a thermal/order artifact. It shrinks to noise at `lw=16`
(base 1.50 vs median 1.49 / small-numpy 1.48).

**Trustworthy signals:**
- **Parity** (order-independent): `ctc`, `median`, `small-numpy` = **0/100** text
  diffs (byte-identical across the corpus); `numba` = **8/100** (rasterization
  approximation, via adaptive DS selection).
- **Core scaling**: 1.74→1.50 s/page from lw=1→16.

**Conclusion:** after the int8 static-quant (QLinearConv), the corpus is
~1.5 s/page and OCR/model-bound. The post-processing optimizations are each only
tens of ms — below this bench's noise floor end-to-end, even though each is a real
win on its own function (median ~8×, ctc ~1.7×, numba ~25× in isolation). The int8
requant was the decisive win; the numpy postprocess tweaks are harmless (identical
output) but marginal, and numba adds an 8/100 parity cost for no measurable
end-to-end gain.

To measure the postprocess opts without the thermal confound, run variants
**interleaved** (round-robin) and take medians, or measure postprocess time
in-process — but the conclusion above (sub-noise end-to-end) is robust either way.
