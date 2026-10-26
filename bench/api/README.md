# API benchmark — legacy (ProArc/PERO) vs new (taas client)

Two clients, one corpus, one stack. The question is not "is TuzkaOCR fast" — that's
[`../PERF_REPORT.md`](../PERF_REPORT.md) — but **how much throughput the client
protocol itself costs**, which is where ProArc sits today.

One entry point:

```sh
make bench                          # enabled matrices, docker-compose stack
make bench ONLY=serial-per-page     # one matrix
make bench TARGET=k3d N=25          # against the k3d/k3s deploy, 25 pages per case
make bench DRY_RUN=1                # print the plan + its cost, run nothing
```

| File | |
|---|---|
| `scenarios.toml` | targets and parameter sweeps — **the file you edit** |
| `run_scenarios.py` | expands the sweeps, drives the runs, writes `RESULTS_API.md` |
| `legacy_proarc.py` | the legacy PERO API via `compat`, driven exactly as ProArc drives it |
| `new_client.py` | the taas API via `clients/python` (`taas_client`), WebSocket push |
| `common.py` | corpus discovery, user provisioning, timing, reporting |
| `latency-proxy.sh` | injects a simulated network RTT in front of the stack |
| `mzk.pids.txt` | the 200 MZK PIDs the corpus is built from |
| `out/` | per-case JSON (generated, gitignored) |

Either script also runs standalone for a one-off:

```sh
python bench/api/legacy_proarc.py --n 10 --concurrency 1
python bench/api/new_client.py --n 10 --serial
```

## The corpus

The images are **not** in the repo (`test-data/` is gitignored bar one sample) — only
the PID list is, so anyone can rebuild the same corpus and compare against
`RESULTS_API.md`.

Download the pages listed in `mzk.pids.txt` from your Kramerius instance into
`test-data/`, named **`<uuid>_image.jpeg`** — the `uuid:` prefix stripped, one file per
PID:

```
bench/api/mzk.pids.txt          test-data/
uuid:00074b00-113e-11e4-...  -> 00074b00-113e-11e4-..._image.jpeg
```

That's the pattern every bench in this repo globs for, and what the engine benches
([`../BENCHMARKS.md`](../BENCHMARKS.md)) already use — so both sets of numbers describe
the same pages.

**200 pages, chosen for spread.** The list is size-stratified rather than arbitrary:
pages sorted by file size, then taken at evenly spaced ranks with both endpoints kept, so
the largest and smallest pages are exercised and nothing clusters at the median. Sorting
on bytes rather than pixels is deliberate — these scans are all near the same dimensions
(p25–p75 is 3.3–3.7 MP), so byte size is what actually tracks text density, and text
density is what drives OCR time and ALTO size.

| pages | download | mean | median | range |
|---|---|---|---|---|
| 200 | 118 MB | 0.589 MB | 0.59 MB | 0.05 – 1.92 MB |

200 keeps a full sweep to a sane wall time while still spanning the range; `N=` trims it
further per run. Note the list controls the **download**, not the run — the scripts glob
`test-data/` and `n` caps how many are used, so `n` above what is on disk does not error,
it just silently runs everything there.

## What "as ProArc drives it" means

Ported from ProArc's
[`PeroOcrProcessor`](https://github.com/proarc/proarc/blob/master/proarc-common/src/main/java/cz/cas/lib/proarc/common/process/external/PeroOcrProcessor.java)
and its caller `PeroProcess.generateMultipleAlto()`. The behaviours that dominate the
numbers are reproduced deliberately:

| ProArc does | Consequence |
|---|---|
| One `post_processing_request` per page, carrying a single image | the request/image batching in the API is never used |
| `for (File file : folder.listFiles())`, and `process()` blocks | pages are strictly serial — one page in flight, ever |
| `HttpClients.createDefault()` **inside** `process()` | new connection per page, nothing pooled |
| Polls by re-requesting `download_results/.../txt` every 3 s | wall time quantises to 3 s; each poll is a full HTTP round trip |
| Downloads txt (the poll) then alto | 2 result downloads per page |
| `getName().split("\\.")[0]` for the image name | `sig.0001.jpeg` becomes `sig` — two such pages in a folder collide |

So per page: **1 open + 1 upload + N polls + 1 alto download**, serial. At a 60 s page
and a 3 s poll that's ~24 requests and ~21 wasted round trips per page.

## The new-client side

All pages are submitted up front and results come back over one `/ws` connection — per
page just **1 submit + 1 presigned fetch per artifact**, no polling. The `done` frame
carries `alto_url` / `txt_url`; the client GETs them (the SigV4 signature is the auth)
and zstd-decompresses in the callback.

## Both sides must end up with both artifacts

Neither client is "done" with a page until it holds the **decompressed ALTO and text**,
and both write the pair to `out/artifacts/<mode>/` as `<stem>.xml` + `<stem>.txt` —
which is also exactly what ProArc does. A page that returns only one format **fails the
case** rather than quietly making one side cheaper. The disk write is inside the timed
region, because it's real work both sides do.

Note the two paths decompress in *different places*, and that's inherent, not a
benchmark artifact:

- **legacy** never touches a presigned URL — `compat` pulls the artifact over the
  internal network (`/result/{fmt}/download`) and decompresses **server-side**, handing
  the client raw bytes.
- **new** fetches from the public MinIO endpoint and decompresses **client-side**.

Both hops sit behind the latency proxy, which is why the results port is proxied too.

### A known ceiling in the sync client

`TaasClient._ws_listen` awaits `_handle_event` per frame, and the fetch + decompress
happen inside that handler — so result handling is **serialized**: page 2's `done` frame
waits while page 1 downloads. At high RTT the results drain at ~2 round trips each,
sequentially, no matter how many jobs are in flight, and `--submit-concurrency` doesn't
help because it only widens the submit side. Worth knowing before reading the
all-in-flight latency column at 200 ms.

(The per-artifact `httpx.AsyncClient` that used to compound this — a fresh handshake per
result — is fixed: one pooled client now lives for the life of the socket.
`AsyncTaasClient` and the Java client already reused theirs.)

## Two comparisons, and which number to read

The two clients pace work differently, so there are two honest framings and the scenario
file runs both:

**`protocol` — throughput.** Legacy serial (as ProArc is) vs new all-in-flight. Read
**`pages/min`**. This is the throughput a ProArc-style integration leaves on the table.
Don't read per-page latency here: the new run submits everything immediately, so its
latency absorbs queue time, while the serial loop never has a queue to wait in.

**`serial-per-page` — per-page cost.** `serial = true` puts **one page in flight on both
sides** (`new_client.py --serial` submits a page, blocks for its result, then moves on).
Neither side queues, so **`lat mean` is directly comparable** and the gap is pure protocol
overhead: poll quantisation, the extra round trips, the per-page connection setup. Wall
time is roughly equal here by construction — the latency column is the finding.

The `legacy-parallelism` matrix prices what ProArc *gives up* by being serial. It is not
what ProArc does; `concurrency=1` is the honest baseline.

## Scenario file

Each `[[matrix]]` block is a cartesian product — list values are swept, scalars are
fixed, anything unset falls back to `[defaults]`:

```toml
[[matrix]]
name = "network-distance"
mode = ["legacy", "new"]
latency_ms = [0, 20, 60, 200]
```

Sweepable parameters are the sub-scripts' own flags: `n`, `images`, `timeout`,
`stock_limits`, plus `concurrency` / `poll_seconds` / `engine` / `reuse_connections`
(legacy) and `fmt` / `domain` / `submit_concurrency` / `serial` (new). A parameter
belonging to the other mode is skipped for that case, so one block can set both — that's
how `serial = true` paces both sides (it's a no-op for legacy, already serial by
construction). A typo, on the other hand, is an error.

`enabled = false` marks a long-running block as opt-in — naming it in `ONLY=` runs it:

| opt-in matrix | asks |
|---|---|
| `poll-vs-distance` | does a faster poll still help over a WAN, or do the two knobs fight? |
| `serial-network-distance` | per-page protocol cost by distance, with queueing removed |
| `corpus-scale` | does the ratio hold over a long run (storage, queue depth, GPU arena)? |
| `stock-rate-limits` | where the default GCRA limits cap legacy, whose polls spend query budget |
| `legacy-connection-reuse` | what ProArc's `HttpClients.createDefault()` per page costs |
| `new-result-formats` | one artifact vs two, separated from push-vs-poll |

Run the plan first — `make bench DRY_RUN=1 ONLY=<name>` prints the case count and a wall
time estimate. On a ~1 s/page GPU engine the opt-in blocks land between 4 min
(`stock-rate-limits`) and ~60 min (`corpus-scale`); the default sweep is ~2.5 h.

Every case gets a **fresh throwaway user with rate limits lifted**, so the GCRA limiter
doesn't distort throughput. The `stock-rate-limits` matrix keeps the default limits —
worth running once, because legacy's poll loop spends the user's *query* budget on pages
that aren't ready yet, and that ceiling is a real operational finding.

## Simulating network latency

ProArc does not run next to the OCR service, and a chatty protocol is exactly what a WAN
punishes. `latency-proxy.sh` puts [toxiproxy](https://github.com/Shopify/toxiproxy) on
the socket so **every** channel pays the same RTT.

It has to be a TCP proxy rather than a `--latency-ms` sleep in the scripts. Legacy is
pure request/response, so a sleep would model it correctly — but the new client also
receives over a WebSocket and downloads from presigned MinIO URLs. Delaying only what the
bench script itself calls would tax legacy honestly and let the new client off, flattering
precisely the side under test.

`run_scenarios.py` owns the proxy's lifecycle: it starts it if any case needs delay,
`set`s the RTT per case, and tears it down at the end. Two further points of method:

- If *any* case asks for delay, **every** case is routed through the proxy, the
  zero-latency ones with the delay set to 0 — otherwise the proxy's own overhead would
  land on only half the comparison.
- `latency_ms` is the **total added round trip**, split half upstream / half downstream;
  `jitter_ms` varies each half. Calibration: same DC ≈ 1 ms, same city ≈ 5–15 ms,
  cross-country ≈ 40 ms, intercontinental ≈ 150–250 ms.

The two deployments constrain ports differently, so each target profile in
`scenarios.toml` carries its own wiring plus a one-time prerequisite.

**k3d** — the proxy takes over the published ports and the real services shift, so
presigned URLs signed for `localhost:9100` keep working with no chart change:

```sh
make k3d-forward-stop
make k3d-forward-bench     # real services -> :18080 / :18081 / :19100
make bench TARGET=k3d
```

**compose** — the proxy adds delayed ports beside the published ones, so the presigned
URL host has to move (once):

```sh
# .env.app
MINIO_RESULTS_PUBLIC_URL=http://localhost:19010
docker compose up -d api
make bench
```

Revert that to `http://localhost:9010` when you're done with latency runs. If the
upstream isn't where the profile expects it, the runner says so and prints the fix before
running anything.

Because legacy pays the RTT on every poll and the new client pays it about three times
per page, the gap widens roughly linearly with distance — that spread is the point of the
exercise.

## Output

`RESULTS_API.md` (a markdown table per matrix) plus per-case JSON in `out/`, each
carrying the full per-page timings:

```
### serial-per-page
| case | mode | pages | ok | fail | wall s | pages/min | lat mean | lat p95 | engine | queue | overhead | requests | polls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| mode=legacy | legacy | 10 | 10 | 0 | 602.1 | 1.0 | 60.2 | 64.0 | 55.0 | 0.1 | 5.1 | 240 | 200 |
| mode=new    | new    | 10 | 10 | 0 | 561.4 | 1.1 | 56.1 | 58.0 | 55.0 | 0.1 | 1.0 |  30 |   0 |
```

(Illustrative shape, not measured numbers.) `engine` / `queue` / `overhead` decompose
`lat mean`: what the OCR engine spent, what the job waited in queue, and what is left —
the protocol's own cost, which is the column the whole exercise is about.

## Note on JPEG 2000

taas's default `ALLOWED_EXTENSIONS` is `[".tif",".tiff",".jpg",".jpeg",".png"]`, so
`.jp2` uploads are rejected with 400 before reaching the engine — even though ProArc's
`getContentType()` explicitly supports `image/jp2` and OpenCV decodes it fine. The
benchmarks detect a JPEG 2000 corpus and print the fix. Irrelevant for the `_image.jpeg`
corpus above, but a real gap if ProArc ever sends JP2.
