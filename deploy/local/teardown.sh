#!/usr/bin/env bash
# Tear down the local taas test environment created by setup.sh.
# By default: stop the host engine+tunnel, uninstall the chart, delete PVCs — but
# KEEP the k3d cluster (fast to redeploy into). Pass --cluster to also delete it.
set -euo pipefail

CLUSTER="${CLUSTER:-taas}"
RELEASE="${RELEASE:-taas}"
DELETE_CLUSTER=0
[ "${1:-}" = "--cluster" ] && DELETE_CLUSTER=1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Stopping host engine + tunnel (docker compose)"
( cd "$ROOT/deploy/local/engine-proxy" && docker compose down -v 2>/dev/null ) || echo "  (engine-proxy not running)"

if [ "$DELETE_CLUSTER" = "1" ]; then
  say "Deleting k3d cluster '$CLUSTER' (removes everything)"
  k3d cluster delete "$CLUSTER" || echo "  (cluster '$CLUSTER' not found)"
  echo "Done."
  exit 0
fi

# Target the cluster for the in-cluster cleanup below.
kubectl config use-context "k3d-$CLUSTER" >/dev/null 2>&1 || true

say "Uninstalling the taas Helm release"
helm uninstall "$RELEASE" 2>/dev/null || echo "  (release '$RELEASE' not installed)"

say "Waiting for pods + CNPG cluster to clear"
for _ in $(seq 1 40); do
  n=$(kubectl get pods --no-headers 2>/dev/null | wc -l)
  c=$(kubectl get cluster --no-headers 2>/dev/null | wc -l)
  [ "$n" = "0" ] && [ "$c" = "0" ] && break
  sleep 3
done

say "Deleting leftover PVCs (Postgres / MinIO / Redis data)"
kubectl delete pvc --all --ignore-not-found 2>&1 | sed 's/^/  /' || true

cat <<EOF

Done. The k3d cluster '$CLUSTER' (and the CloudNativePG operator) are still up, so
you can redeploy fast with:
  make local-deploy-up           # rebuilds images + reinstalls the chart + engine

To remove the cluster entirely:
  make local-deploy-clean
EOF
