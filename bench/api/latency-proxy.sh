#!/usr/bin/env bash
# Put a configurable network delay between the benchmark client and the stack.
#
# Why a TCP proxy and not a --latency-ms sleep in the bench scripts: the two APIs
# don't use the same channels. Legacy is pure request/response, so a client-side
# sleep would model it fine — but the new client also takes results over a
# WebSocket push and fetches artifacts from presigned MinIO URLs. Delaying only
# what the bench script itself calls would tax legacy honestly and let the new
# client off, i.e. flatter exactly the side we're trying to prove. Toxiproxy sits
# on the socket, so every byte of all three channels pays the same RTT.
#
# The trick that makes this transparent: the proxy takes over the *published*
# ports (8080/8081/9100) and the real services move to shifted ones. Presigned
# result URLs are SigV4-signed for http://localhost:9100 (minio.results.publicUrl),
# so they keep working, delay included — no chart change, no re-deploy.
#
#   k3d:  make k3d-forward-stop
#         API_PORT=18080 COMPAT_PORT=18081 RESULTS_PORT=19100 make k3d-forward
#         bench/latency-proxy.sh up
#
# Usage:
#   latency-proxy.sh up       start the proxy and apply LATENCY_MS
#   latency-proxy.sh set      change the delay on a running proxy (no restart)
#   latency-proxy.sh status   show the configured proxies and toxics
#   latency-proxy.sh down     remove the proxy container
#
# Env:
#   LATENCY_MS   total added round-trip, split half up / half down  (default 40)
#   JITTER_MS    +/- random variation on each half                  (default 0)
#   LISTEN_API / LISTEN_COMPAT / LISTEN_RESULTS   ports the client talks to
#   UP_API / UP_COMPAT / UP_RESULTS               where the real services listen
set -euo pipefail

LATENCY_MS="${LATENCY_MS:-40}"
JITTER_MS="${JITTER_MS:-0}"

LISTEN_API="${LISTEN_API:-8080}"
LISTEN_COMPAT="${LISTEN_COMPAT:-8081}"
LISTEN_RESULTS="${LISTEN_RESULTS:-9100}"

UP_API="${UP_API:-127.0.0.1:18080}"
UP_COMPAT="${UP_COMPAT:-127.0.0.1:18081}"
UP_RESULTS="${UP_RESULTS:-127.0.0.1:19100}"

CONTAINER="${CONTAINER:-taas-bench-latency}"
IMAGE="${TOXIPROXY_IMAGE:-ghcr.io/shopify/toxiproxy:2.12.0}"
ADMIN="http://127.0.0.1:8474"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# Half each way, so a request/response pair pays LATENCY_MS in total.
half() { echo $(( LATENCY_MS / 2 )); }

api() {  # api <method> <path> [json]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$ADMIN$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$ADMIN$path"
  fi
}

ensure_running() {
  curl -sf "$ADMIN/version" >/dev/null 2>&1 || die "proxy not running — run '$0 up' first"
}

add_proxy() {  # add_proxy <name> <listen-port> <upstream>
  local name="$1" port="$2" upstream="$3"
  api DELETE "/proxies/$name" >/dev/null 2>&1 || true
  api POST /proxies \
    "{\"name\":\"$name\",\"listen\":\"0.0.0.0:$port\",\"upstream\":\"$upstream\",\"enabled\":true}" \
    >/dev/null || die "could not create proxy $name (is :$port already bound?)"
  echo "  $name  localhost:$port  ->  $upstream"
}

apply_toxics() {  # apply_toxics <name>
  local name="$1" ms
  ms="$(half)"

  for stream in upstream downstream; do
    api DELETE "/proxies/$name/toxics/latency_$stream" >/dev/null 2>&1 || true
    [ "$LATENCY_MS" -eq 0 ] && continue
    api POST "/proxies/$name/toxics" \
      "{\"name\":\"latency_$stream\",\"type\":\"latency\",\"stream\":\"$stream\",
        \"attributes\":{\"latency\":$ms,\"jitter\":$JITTER_MS}}" >/dev/null \
      || die "could not add $stream toxic to $name"
  done
}

up() {
  command -v docker >/dev/null || die "docker not found"

  if curl -sf "$ADMIN/version" >/dev/null 2>&1; then
    say "proxy already running — reconfiguring"
  else
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    say "Starting $IMAGE (host network)"
    docker run -d --name "$CONTAINER" --network host "$IMAGE" \
      -host 0.0.0.0 >/dev/null || die "could not start toxiproxy"

    for _ in $(seq 1 30); do
      curl -sf "$ADMIN/version" >/dev/null 2>&1 && break
      sleep 0.5
    done
    curl -sf "$ADMIN/version" >/dev/null 2>&1 || die "toxiproxy did not come up on :8474"
  fi

  say "Proxies"
  add_proxy taas-api     "$LISTEN_API"     "$UP_API"
  add_proxy taas-compat  "$LISTEN_COMPAT"  "$UP_COMPAT"
  add_proxy taas-results "$LISTEN_RESULTS" "$UP_RESULTS"

  set_latency
}

set_latency() {
  ensure_running
  for name in taas-api taas-compat taas-results; do
    apply_toxics "$name"
  done
  say "Added RTT: ${LATENCY_MS}ms (jitter ${JITTER_MS}ms) on api, compat and result downloads"
}

status() {
  ensure_running
  api GET /proxies | python3 -m json.tool
}

down() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 && say "removed $CONTAINER" || say "not running"
}

case "${1:-up}" in
  up)     up ;;
  set)    set_latency ;;
  status) status ;;
  down)   down ;;
  *)      echo "usage: $0 [up|set|status|down]" >&2; exit 2 ;;
esac
