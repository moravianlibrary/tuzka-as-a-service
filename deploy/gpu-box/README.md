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
   └─ reverse tunnel ◄───────────────── <release>-tunnel-engine-gpu1:8000
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
tunnelOcrEngines:
  - name: gpu1
    remotePort: 8000        # unique per box; must match the box's REMOTE_PORT
```

`helm upgrade` and note a node IP (`kubectl get nodes -o wide`) the box can reach on
`nodePort`.

## Box side

Prereqs: Docker + Compose, and (for `OCR_DEVICE=cuda`) the NVIDIA Container Toolkit.

```sh
cp .env.example .env
# edit .env: FRP_SERVER_ADDR (node IP), FRP_TOKEN (== secrets.frpToken),
#            OCR_API_KEY (== secrets.ocrEngineApiKey), REMOTE_PORT (== remotePort)
```

The engine comes from one of two run modes, set by `COMPOSE_PROFILES` in `.env`:

```sh
# Option A — prebuilt image (default, COMPOSE_PROFILES=registry):
docker compose up -d

# Option B — build from a local checkout in ./TuzkaOCR (COMPOSE_PROFILES=build):
git clone <tuzkaocr-repo> ./TuzkaOCR      # or symlink an existing checkout
docker compose --profile build up -d --build

docker compose logs -f frpc               # expect "start proxy success"
```

`frpc.toml` is rendered from the `.env` (frp env templating) — don't edit it directly.

## Verify end-to-end

```sh
# from inside the cluster:
kubectl run curl --rm -it --image=curlimages/curl -- \
  curl -sf http://<release>-tunnel-engine-gpu1:8000/healthz
```

Then submit a job through the taas API and confirm it returns ALTO produced on the box.

## Notes

- **Keys must match**: box `FRP_TOKEN` == cluster `secrets.frpToken`; box `OCR_API_KEY`
  == cluster `secrets.ocrEngineApiKey`.
- **`REMOTE_PORT` is unique per box** and must equal that engine's
  `tunnelOcrEngines[].remotePort`. Add more boxes by adding entries (each a distinct
  `remotePort`) and running this stack on each with the matching `REMOTE_PORT`.
- **Debugging**: failures show up in `frpc` (box) and `<release>-frps` (cluster) logs,
  not in Kubernetes endpoints — the Service is only a port-alias to the frps socket.
- **NodePort reachability**: the node IP must be routable from the box and `nodePort`
  open in any firewall. On cloud / MetalLB, prefer `tunnel.service.type: LoadBalancer`
  and point `FRP_SERVER_ADDR`/`FRP_SERVER_PORT` at the LB.
