# Clean variant bench — interleaved, median of 5 rounds

Corpus: 100 pages · 1 model load/batch · `ocr_threads=1 line_workers=1` · rotating order per round (thermal/order-controlled)

| variant | median s/page | min | max | vs base |
|---|---|---|---|---|
| base | 1.967 | 1.765 | 2.022 | — |
| ctc | 2.023 | 1.877 | 2.080 | +2.8% |
| median | 1.935 | 1.922 | 2.011 | -1.6% |
| numba | 2.025 | 2.003 | 2.032 | +2.9% |
| small-numpy | 1.954 | 1.900 | 1.997 | -0.6% |

Raw per-round s/page samples:
- base: 1.765, 1.967, 1.999, 1.962, 2.022
- ctc: 1.877, 2.067, 1.977, 2.023, 2.080
- median: 1.935, 1.996, 1.923, 1.922, 2.011
- numba: 2.019, 2.032, 2.003, 2.028, 2.025
- small-numpy: 1.900, 1.997, 1.954, 1.921, 1.973

## Reading the clean results

With thermal/order controlled (rotating interleave, median of 5 rounds), all
variants land within **~±3% of base**, which is inside the per-variant sample
spread (~±5–14%). At `line_workers=1` the page is OCR/model-bound (~1.97 s/page)
and the post-processing optimizations are **not end-to-end distinguishable** from
base.

- A delta like `ctc +2.8%` (~0.055 s/page) is *larger than the whole CTC step*
  (tens of ms), so it is noise, not the code. The same bounds `median -1.6%` and
  `small-numpy -0.6%` — real-but-tiny at most, swamped by run noise.
- `numba +2.9%` is the only delta with a plausible mechanism (per-process import +
  idle thread pool), consistent with prior findings — and it carries an 8/100
  text-parity cost. Drop numba.

**Overall:** the int8 static-quant (QLinearConv) was the decisive win (page now
~1.5–2 s vs ~24 s on the old dynamic model). Post-processing micro-opts are
sub-noise end-to-end on a 1-CPU engine; ship the byte-identical numpy ones if
desired (harmless), skip numba.
