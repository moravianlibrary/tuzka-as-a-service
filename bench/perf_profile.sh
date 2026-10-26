#!/usr/bin/env bash
# Profile a k3d bench-taas run from ephemeral containers, leaving worker memory untouched.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${TARGET:-k3d}" != k3d ]; then
  echo "bench-profile requires TARGET=k3d" >&2
  exit 2
fi
export KUBECONFIG="$HOME/.kube/k3d-taas.yaml"
if [ "$(kubectl config current-context)" != k3d-taas ]; then
  echo "bench-profile requires the local k3d-taas context" >&2
  exit 2
fi

OUT="${OUT:-bench/perf_out/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
sidecar="pyspy-$(date +%s)"
components=(api worker-submit worker-poller)
declare -A pods targets

for component in "${components[@]}"; do
  pods[$component]="$(kubectl get pod -l "app.kubernetes.io/component=$component" -o jsonpath='{.items[0].metadata.name}')"
  if [ -z "${pods[$component]}" ]; then
    echo "No pod for $component" >&2
    exit 1
  fi
  targets[$component]=worker
  [ "$component" = api ] && targets[$component]=api
  kubectl debug "${pods[$component]}" --image=python:3.12-slim \
    --target="${targets[$component]}" --profile=sysadmin --container="$sidecar" \
    -- sh -c 'pip install -q --disable-pip-version-check py-spy; sleep 3600' \
    >"$OUT/$component-debug.log" 2>&1
  ready=0
  for _ in $(seq 1 60); do
    if kubectl exec "${pods[$component]}" -c "$sidecar" -- py-spy --version \
      >"$OUT/$component-pyspy-version.txt" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 1
  done
  if [ "$ready" -ne 1 ]; then
    echo "py-spy did not start in ${pods[$component]}" >&2
    exit 1
  fi
done

started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TARGET=k3d OUT="$OUT" USERS="${USERS:-50}" RATE="${RATE:-10}" \
  DURATION="${DURATION:-2m}" bash bench/perf_taas.sh >"$OUT/perf_taas.log" 2>&1 &
perf_pid=$!
for _ in $(seq 1 60); do
  if grep -q '==> Locust:' "$OUT/perf_taas.log"; then
    break
  fi
  if ! kill -0 "$perf_pid" 2>/dev/null; then
    echo "bench-taas stopped before Locust started; see $OUT/perf_taas.log" >&2
    wait "$perf_pid" || true
    exit 1
  fi
  sleep 1
done
if ! grep -q '==> Locust:' "$OUT/perf_taas.log"; then
  echo "Locust did not start; see $OUT/perf_taas.log" >&2
  wait "$perf_pid" || true
  exit 1
fi

profile_pids=()
for component in "${components[@]}"; do
  kubectl exec "${pods[$component]}" -c "$sidecar" -- sh -c \
    'py-spy record -d 60 -r 50 -o /tmp/taas-cpu.svg --pid 1 && py-spy record -d 60 -r 50 --idle -o /tmp/taas-idle.svg --pid 1' \
    >"$OUT/$component-profile.log" 2>&1 &
  profile_pids+=("$!")
done

profile_status=0
for pid in "${profile_pids[@]}"; do
  wait "$pid" || profile_status=1
done
perf_status=0
wait "$perf_pid" || perf_status=1

for component in "${components[@]}"; do
  for mode in cpu idle; do
    kubectl cp -c "$sidecar" "${pods[$component]}:/tmp/taas-$mode.svg" \
      "$OUT/$component-$mode.svg" || profile_status=1
  done
done
for component in worker-submit worker-poller; do
  kubectl logs "${pods[$component]}" -c worker --since-time="$started" \
    | awk '/stage=/' >"$OUT/$component-stages.log"
done

kubectl exec -i taas-clickhouse-0 -- sh -c \
  'clickhouse-client --password "$CLICKHOUSE_PASSWORD"' \
  >"$OUT/stage_totals.tsv" <<SQL
SELECT labels['worker'] AS worker, labels['stage'] AS stage,
       count() AS windows, round(sum(value), 6) AS total_s
FROM taas.live_metrics
WHERE metric = 'worker_stage_seconds' AND ts >= parseDateTimeBestEffort('$started')
GROUP BY worker, stage ORDER BY worker, stage FORMAT TSVWithNames
SQL

for component in "${components[@]}"; do
  # Only the temporary debug containers run this exact sleep command.
  kubectl exec "${pods[$component]}" -c "$sidecar" -- sh -c '
    for pid in $(pidof sleep); do
      cmd=$(tr "\000" " " <"/proc/$pid/cmdline")
      [ "$cmd" = "sleep 3600 " ] && kill "$pid"
    done
  ' >/dev/null 2>&1 || true
done

echo "Profiles and stage totals: $OUT"
if [ "$profile_status" -ne 0 ] || [ "$perf_status" -ne 0 ]; then
  echo "bench-profile failed: bench-taas=$perf_status py-spy=$profile_status; see $OUT" >&2
  exit 1
fi
