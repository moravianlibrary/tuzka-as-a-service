# Off-cluster GPU engine (reverse tunnel)

Run a TuzkaOCR engine on a GPU box that **can't accept inbound connections** (behind
NAT/firewall) but **can dial out** to the cluster. The box opens a reverse tunnel into
the cluster's `frps` server; taas then uses the engine as if it were a normal
in-cluster backend. No taas changes — the remoteness is transparent.

```
GPU box (outbound only)                 cluster (taas Helm chart)
 tuzkaocr :8000                          <release>-frps Deployment
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
- **Debugging**: failures show up in `frpc` (box) and `<release>-frps` (cluster) logs,
  not in Kubernetes endpoints — the Service is only a port-alias to the frps socket.
- **NodePort reachability**: the node IP must be routable from the box and `nodePort`
  open in any firewall. On cloud / MetalLB, prefer `tunnel.service.type: LoadBalancer`
  and point `FRP_SERVER_ADDR`/`FRP_SERVER_PORT` at the LB.

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
