#!/usr/bin/env bash
# Stand up the full taas stack on a local k3d cluster, ready for a REAL host engine
# tunneled in via frps. Idempotent-ish: safe to re-run. See deploy/local/README.md.
set -euo pipefail

CLUSTER="${CLUSTER:-taas}"
RELEASE="${RELEASE:-taas}"
NODEPORT="${NODEPORT:-32700}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VALUES="$ROOT/deploy/local/values.local.yaml"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "1/6  Ensuring k3d is installed"
if ! command -v k3d >/dev/null 2>&1; then
  echo "k3d not found. Install it (no sudo) with:"
  echo "  mkdir -p ~/.local/bin && curl -sSL https://github.com/k3d-io/k3d/releases/latest/download/k3d-linux-amd64 -o ~/.local/bin/k3d && chmod +x ~/.local/bin/k3d"
  echo "  # make sure ~/.local/bin is on PATH, then re-run this script."
  echo "Or with the official script (uses sudo): curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash"
  exit 1
fi

say "2/6  Creating k3d cluster '$CLUSTER' (host :$NODEPORT -> frps NodePort)"
if k3d cluster list 2>/dev/null | grep -qE "^$CLUSTER\b"; then
  echo "Cluster '$CLUSTER' already exists, reusing it."
else
  k3d cluster create "$CLUSTER" \
    --servers 1 --agents 0 \
    -p "${NODEPORT}:${NODEPORT}@server:0"
fi
kubectl config use-context "k3d-$CLUSTER"

say "3/6  Installing the CloudNativePG operator"
helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
helm repo update cnpg >/dev/null
helm upgrade --install cnpg cnpg/cloudnative-pg \
  -n cnpg-system --create-namespace --wait

say "4/6  Building taas images and importing them into k3d"
docker build -t taas-api:latest    -f "$ROOT/api.Containerfile"    "$ROOT"
docker build -t taas-worker:latest -f "$ROOT/worker.Containerfile" "$ROOT"
docker build -t taas-compat:latest -f "$ROOT/compat/Containerfile" "$ROOT/compat"
k3d image import taas-api:latest taas-worker:latest taas-compat:latest -c "$CLUSTER"

say "5/6  Installing the taas chart"
helm upgrade --install "$RELEASE" "$ROOT/deploy/helm/taas" \
  -f "$VALUES" --wait --timeout 5m

say "6/6  Waiting for the backend-register hook job to finish"
kubectl wait --for=condition=complete --timeout=180s \
  "job/${RELEASE}-backend-register" || \
  echo "(register job not complete yet — it retries until the API + engine are up)"

cat <<EOF

Done (cluster + chart). Next:
  1) Start the host engine + tunnel (uses the tuzkaocr:local-* image you built):
       docker compose -f deploy/local/engine-proxy/compose.yaml up -d
     (Tip: 'make local-deploy-up' runs this whole step + the above for you.)
  2) Port-forward the services + open the UIs:
       make local-deploy-forward-up
  3) Verify the tunnel + run a job — see deploy/local/README.md "Verify".
EOF
