# Off-cluster GPU engine (reverse tunnel)

Run a TuzkaOCR engine on a GPU box that **can't accept inbound connections** (behind
NAT/firewall) but **can dial out** to the cluster. The box opens a reverse tunnel into
the cluster's `frps` server; taas then uses the engine as if it were a normal
in-cluster backend. No taas changes — the remoteness is transparent.

```
GPU box (outbound only)                 cluster (taas Helm chart)
 tuzkaocr :8000                          <release>-frps Deployment
   ▲
 gate :8000  (pass-through proxy)
   ▲
 frpc ──dials node:32700──────────────►  control :7000
   ▲                                      opens remotePort :8000 on the frps pod
   └─ reverse tunnel ◄───────────────── <release>-tunnel-engine-box1-gpu1:8000
                                            └─► taas submit / poller workers
```

## Cluster side (once)

In the taas Helm values:

```yaml
tunnel:
  enabled: true
  service: { type: NodePort, nodePort: 32700 }   # or LoadBalancer
secrets:
  frpToken: "<a-strong-shared-secret>"
  ocrEngineApiKey: "<engine-api-key>"
tunnelBoxes:
  - name: box1
    engines:
      - name: gpu1
        remotePort: 8000    # unique across all boxes; must match the box's REMOTE_PORT
```

`helm upgrade` and note a node IP (`kubectl get nodes -o wide`) the box can reach on
`nodePort`.

## Box side

Prereqs: Docker + Compose.

```sh
cp .env.example .env
# edit .env: FRP_SERVER_ADDR (node IP), FRP_TOKEN (== secrets.frpToken),
#            OCR_API_KEY (== secrets.ocrEngineApiKey), REMOTE_PORT (== remotePort)
```

## Run

CPU box (no NVIDIA toolkit required):

    cp .env.example .env   # fill in FRP_*, OCR_API_KEY, ENGINE_NAME, REMOTE_PORT; keep OCR_DEVICE=cpu
    docker compose up -d

GPU box (needs nvidia-container-toolkit; set OCR_DEVICE=cuda and a CUDA image/Dockerfile.gpu in .env):

    docker compose -f compose.yaml -f compose.gpu.yaml up -d

`frpc.toml` is rendered from the `.env` (frp env templating) — don't edit it directly.

## Verify end-to-end

```sh
# from inside the cluster:
kubectl run curl --rm -it --image=curlimages/curl -- \
  curl -sf http://<release>-tunnel-engine-box1-gpu1:8000/healthz
```

Then submit a job through the taas API and confirm it returns ALTO produced on the box.

## Notes

- **Keys must match**: box `FRP_TOKEN` == cluster `secrets.frpToken`; box `OCR_API_KEY`
  == cluster `secrets.ocrEngineApiKey`.
- **`REMOTE_PORT` is unique across all boxes** and must equal that engine's
  `tunnelBoxes[].engines[].remotePort`. Add more boxes by adding `tunnelBoxes` entries
  (each engine/exporter a distinct `remotePort`) and running this stack on each with the
  matching `REMOTE_PORT`.
- **Engine version**: TuzkaOCR **v1.7.0** is verified against this box — same routes, form
  fields and status payload, and its spool directory is baked into the image, so only the
  tag (`OCR_IMAGE`) or the `./TuzkaOCR` checkout changes. Note that handwritten/Kurrent
  output differs from v1.6.x (unified `rec-H-v6` model); print/kramarky is unaffected.
- **Debugging**: failures show up in `frpc` (box) and `<release>-frps` (cluster) logs,
  not in Kubernetes endpoints — the Service is only a port-alias to the frps socket.
- **NodePort reachability**: the node IP must be routable from the box and `nodePort`
  open in any firewall. On cloud / MetalLB, prefer `tunnel.service.type: LoadBalancer`
  and point `FRP_SERVER_ADDR`/`FRP_SERVER_PORT` at the LB.

## Up-hours: run OCR only outside working hours

A box that has a day job (a work PC, a shared workstation) can serve OCR only in a
weekly window. Two containers implement it, both present by default and both inert while
`OCR_UP_SCHEDULE=always`:

- **`gate`** — nginx between `frpc` and the engine. The tunnel now terminates here.
  Open, it is a pass-through. Closed, it 503s `/healthz` and `POST /api/v1/process` and
  passes everything else through.
- **`scheduler`** — reconciles container state against the schedule once a minute,
  driving the host docker daemon through the socket. It only touches containers labelled
  with this compose project.

Set the window in `.env`:

```sh
TZ=Europe/Prague                                              # the schedule's timezone
OCR_UP_SCHEDULE="Mon-Fri 17:30-07:30; Sat,Sun 00:00-24:00"    # nights + weekends
OCR_DRAIN_MINUTES=10
docker compose up -d        # or restart just the scheduler after an edit
```

Syntax: `<days> <HH:MM>-<HH:MM>` entries separated by `;` (a comma right after a time
range also works, so day lists like `Sat,Sun` stay unambiguous). Days accept ranges and
lists, both wrapping (`Fri-Mon`); an end time at or before the start runs past midnight
into the next day, attributed to the day it *starts* on — `Fri 22:00-06:00` covers
Saturday morning whether or not `Sat` is listed. `always` (default) and `never` are
accepted in place of any window.

**What happens at the boundaries**

| when | what the scheduler does |
| --- | --- |
| window start | opens the gate, starts engine + gate + frpc |
| window end | closes the gate — taas drops the backend within `health_cache_seconds` (10s), so no new pages arrive |
| + `OCR_DRAIN_MINUTES` | stops the engine, releasing the GPU |

The tunnel stays up the whole time, which is the point of the gate: pages the engine is
already working on finish during the drain and the poller harvests them normally
through `/api/v1/status` + `/api/v1/result`. Only pages still unfinished when the drain
expires are given up, and taas requeues those (an unreachable engine is already a
retryable state), so they are redone in the next window, not lost. Queued jobs simply
wait — an off-hours box is an unhealthy backend, not a failing one.

Notes:

- Set `OCR_STOP_TUNNEL=1` to stop `frpc` too, after the engine. Off by default so the
  box's cAdvisor / GPU metrics keep reaching the cluster Prometheus while it's parked.
- The scheduler reconciles what it *observes*, so it recovers on its own after a reboot,
  a suspend/resume, or a manual `docker compose up -d` inside a closed window.
- `docker compose down` stops everything including the scheduler; the box then stays
  down until you bring it back up. `restart: unless-stopped` keeps a stopped engine
  stopped across daemon restarts, which is what the schedule wants.
- To skip the gate entirely (and with it up-hours scheduling), set `FRP_LOCAL_IP=tuzkaocr`
  in `.env` so the tunnel delivers straight to the engine.
- Multi-engine boxes: `compose.multi.example.yaml` has no gate. Add one gate per engine
  (each with its own `gate-state` volume, `frpc.toml` pointing at it) and list the engine
  services in `SCHED_ENGINE_SERVICES`.

## Multiple engines on one box (CPU + GPU mix)

A single box can host several engines (any CPU/GPU mix), each registered as its own
taas backend. One `frpc` multiplexes them — one `[[proxies]]` per engine, each with a
UNIQUE `name` and `remotePort` matching a `tunnelBoxes[].engines[]` entry in the Helm values.

1. In the Helm values, add one `tunnelBoxes` entry for the box with one `engines[]`
   entry per engine (unique `name` + `remotePort`); see deploy/helm/taas/values.yaml.
2. On the box, copy `frpc.multi.example.toml` -> `frpc.toml` and edit names/ports.
3. Use `compose.multi.example.yaml` as a starting point (one engine service per
   engine; GPU services carry the nvidia device reservation, CPU ones don't).
4. `docker compose -f compose.multi.example.yaml up -d`.

## Resource metrics (cAdvisor + GPU exporter)

The box can expose container metrics (cAdvisor) and NVIDIA GPU metrics
(`nvidia_gpu_exporter`) to the cluster Prometheus through the **same** frpc tunnel. A box
has at most one of each regardless of engine count.

1. In the Helm values, add an `exporters[]` list to the box (one `cadvisor` and/or one
   `gpu-exporter` entry, each with a unique `remotePort` + the in-cluster `port`). Set
   `metrics.serviceMonitor.enabled: true` if you run the Prometheus Operator, else scrape
   the `<release>-tunnel-box-<box>-<exporter>` Services statically (see the chart README).
2. On the box (single-engine `compose.yaml`): `cadvisor` runs already; the GPU exporter
   is in `compose.gpu.yaml`. Set `BOX_NAME` and the optional `FRP_CADVISOR_REMOTE_PORT` /
   `FRP_GPU_EXPORTER_REMOTE_PORT` in `.env` to match the `exporters[].remotePort` values —
   the matching `frpc.toml` blocks then activate (they're skipped when the vars are unset).
   > ⚠️ Only set `FRP_GPU_EXPORTER_REMOTE_PORT` when the box is started **with** the GPU
   > overlay (`-f compose.gpu.yaml`) — the `gpu-exporter` service lives only there. Setting
   > it on a plain `compose.yaml` box makes frpc open a tunnel to a host that doesn't
   > exist, so Prometheus scrapes fail silently with no error in the cluster.
3. For a multi-engine box, author `frpc.toml` from `frpc.exporters.example.toml`, which
   shows engine + exporter proxy blocks together.
