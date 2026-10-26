#!/usr/bin/env bash
# Bring the k3d stack to a benchable state, then hand off to the scenario runner.
#
# This is the cold-start path: `make bench` assumes a running stack, and everything
# it needs that *isn't* running yet gets created here. Deliberately idempotent —
# each step checks first, so re-running after a partial failure resumes rather than
# rebuilding. Safe to run when everything is already up (it does nothing).
#
# Split of responsibilities, so neither half guesses:
#   this script      cluster, chart, engine images, host engines  (command sequencing)
#   run_scenarios.py users, backends, port-forwards, proxy, cases  (needs the config)
#
# Port-forwards are NOT started here on purpose: only the runner knows whether the
# plan contains latency cases, and that decides which ports the forwards must use.
#
# Usage:
#   bench/api/bench-up.sh                       # cold start + the default sweep
#   bench/api/bench-up.sh ONLY=protocol N=50    # args are passed through to `make bench`
#   SKIP_BENCH=1 bench/api/bench-up.sh          # just bring the stack up
#
# Env:
#   SKIP_BENCH=1   set up only, do not run the benchmark
#   WITH_GPU=0     skip the GPU engine image (CPU-only rig; ~4.5 GB less to build)
set -euo pipefail
cd "$(dirname "$0")/../.."

SKIP_BENCH="${SKIP_BENCH:-}"
WITH_GPU="${WITH_GPU:-1}"
CLUSTER="${CLUSTER:-taas}"
MIN_FREE_GB="${MIN_FREE_GB:-10}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. Prerequisites --------------------------------------------------------
# Checked up front: each of these fails deep inside a slow step otherwise, and a
# 20-minute image build is a bad place to discover k3d is missing.

say "Checking prerequisites"
for tool in docker k3d kubectl helm; do
  command -v "$tool" >/dev/null || die "$tool not found"
done

free_gb="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
[ "$free_gb" -ge "$MIN_FREE_GB" ] || die \
  "only ${free_gb}G free on / — need ~${MIN_FREE_GB}G (images, cluster storage, MinIO buckets).
   Note the chart's 2Gi PVC requests are NOT enforced: k3d's local-path provisioner is
   hostPath-backed, so a run consumes host disk with nothing to bounce off."
ok "${free_gb}G free on /"

corpus="$(find test-data -maxdepth 1 -name '*_image.jpeg' 2>/dev/null | wc -l)"
[ "$corpus" -gt 0 ] || die \
  "no *_image.jpeg in test-data/ — download the pages listed in bench/api/mzk.pids.txt
   as <uuid>_image.jpeg (see bench/api/README.md)."
ok "corpus: $corpus pages"

# --- 1. Engine images --------------------------------------------------------
# Built from the local TuzkaOCR checkout, never pulled, so the engine matches the
# source tree. The GPU image is ~4.5 GB and by far the slowest step.

if ! docker image inspect tuzkaocr:local-cpu >/dev/null 2>&1; then
  say "Building the CPU engine image (slow, once)"
  make engine-build-cpu
else
  ok "tuzkaocr:local-cpu present"
fi

if [ "$WITH_GPU" = "1" ]; then
  if ! docker image inspect tuzkaocr:local-gpu >/dev/null 2>&1; then
    say "Building the GPU engine image (~4.5 GB, slow, once)"
    make engine-build-gpu
  else
    ok "tuzkaocr:local-gpu present"
  fi
  command -v nvidia-ctk >/dev/null || printf '\033[1;33m  !!\033[0m %s\n' \
    "nvidia-container-toolkit not found — the GPU engine will fail to start.
     Re-run with WITH_GPU=0 for a CPU-only rig."
fi

# --- 2. Cluster + chart ------------------------------------------------------
# setup.sh is itself idempotent-ish, but it always reinstalls the chart, so skip
# it entirely when the cluster is already serving.

if k3d cluster list --no-headers 2>/dev/null | grep -q "^${CLUSTER} "; then
  ok "k3d cluster '${CLUSTER}' exists"
else
  say "Creating the k3d cluster + installing the chart"
  make k3d-up
fi

if ! kubectl get deploy taas-api >/dev/null 2>&1; then
  say "Chart not installed in '${CLUSTER}' — installing"
  make k3d-up
fi

say "Waiting for the API deployment to be available"
kubectl wait --for=condition=available --timeout=300s deploy/taas-api \
  || die "taas-api never became available — check 'kubectl get pods'"
ok "taas-api available"

# --- 3. Host engines ---------------------------------------------------------
# They live outside the cluster and reach it through the frps reverse tunnel, so
# they are ordinary docker compose services, not pods.

engine_compose="deploy/local/engine-proxy/compose.yaml"
running="$(docker compose -f "$engine_compose" ps --services --filter status=running 2>/dev/null | wc -l)"
if [ "$running" -lt 2 ]; then
  say "Starting the host engines + tunnel"
  docker compose -f "$engine_compose" up -d
fi

say "Waiting for an engine to report healthy through the tunnel"
for _ in $(seq 1 60); do
  healthy="$(docker compose -f "$engine_compose" ps --format '{{.Health}}' 2>/dev/null | grep -c healthy || true)"
  [ "${healthy:-0}" -ge 1 ] && break
  sleep 5
done
[ "${healthy:-0}" -ge 1 ] || die "no engine became healthy — check: docker compose -f $engine_compose logs"
ok "$healthy engine(s) healthy"

# --- 4. Hand off -------------------------------------------------------------
# From here the runner owns everything: it starts the port-forwards in whichever
# layout the plan needs, pins backends, manages the latency proxy and runs cases.

if [ -n "$SKIP_BENCH" ]; then
  say "SKIP_BENCH set — stack is up, stopping here"
  echo "  run the benchmark with: make bench TARGET=k3d"
  exit 0
fi

say "Handing off to the scenario runner"
exec make bench TARGET=k3d "$@"
