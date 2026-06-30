#!/usr/bin/env bash
# Manage background port-forwards for the local taas stack.
#   forward.sh up     start forwards (detached) + open the UIs
#   forward.sh down   stop the forwards started by `up`
# Driven by the Makefile: make local-deploy-forward-up / local-deploy-forward-down.
set -euo pipefail

RELEASE="${RELEASE:-taas}"
API_PORT="${API_PORT:-8080}"
COMPAT_PORT="${COMPAT_PORT:-8081}"
RESULTS_PORT="${RESULTS_PORT:-9100}"
PIDFILE="${TMPDIR:-/tmp}/${RELEASE}-local-forward.pids"

cmd="${1:-up}"

down() {
  if [ ! -f "$PIDFILE" ]; then
    echo "No forwards tracked ($PIDFILE not found)."
    return 0
  fi
  echo "Stopping port-forwards..."
  while read -r pid; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null && echo "  killed $pid" || true
  done < "$PIDFILE"
  rm -f "$PIDFILE"
}

up() {
  if [ -f "$PIDFILE" ] && kill -0 "$(head -1 "$PIDFILE")" 2>/dev/null; then
    echo "Forwards already running (PID file $PIDFILE). Run 'make local-deploy-forward-down' first."
    exit 1
  fi
  : > "$PIDFILE"

  fwd() {  # fwd <svc> <local:remote> <label>
    kubectl port-forward "svc/$1" "$2" >/dev/null 2>&1 &
    echo $! >> "$PIDFILE"
    echo "  $3  ->  $2"
  }

  echo "Starting port-forwards (context: $(kubectl config current-context)):"
  fwd "${RELEASE}-api"           "${API_PORT}:8000"     "API + dashboard "
  fwd "${RELEASE}-compat"        "${COMPAT_PORT}:8001"  "legacy compat   "
  fwd "${RELEASE}-minio-results" "${RESULTS_PORT}:9000" "result downloads"

  echo "Waiting for the API to answer on :${API_PORT}..."
  for _ in $(seq 1 30); do
    if curl -sf "http://localhost:${API_PORT}/healthz" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  if [ -z "${ok:-}" ]; then
    echo "API did not come up on :${API_PORT}. Is the stack running? (kubectl get pods)" >&2
    down
    exit 1
  fi

  urls=("http://localhost:${API_PORT}/" "http://localhost:${API_PORT}/docs")
  if command -v xdg-open >/dev/null 2>&1; then
    echo "Opening UIs in your browser..."
    for u in "${urls[@]}"; do xdg-open "$u" >/dev/null 2>&1 || true; done
  else
    echo "xdg-open not found; open these manually:"
    printf '  %s\n' "${urls[@]}"
  fi

  cat <<EOF

Forwards running in the background. Master key: test-master-key
  Dashboard UI   http://localhost:${API_PORT}/
  Swagger docs   http://localhost:${API_PORT}/docs
  Job API        http://localhost:${API_PORT}/api/v1
  Admin API      http://localhost:${API_PORT}/admin       (header X-Master-Key: test-master-key)
  Legacy compat  http://localhost:${COMPAT_PORT}
  MinIO results  http://localhost:${RESULTS_PORT}         (presigned result downloads)

Stop them with: make local-deploy-forward-down
EOF
}

case "$cmd" in
  up)   up ;;
  down) down ;;
  *)    echo "usage: $0 [up|down]" >&2; exit 2 ;;
esac
