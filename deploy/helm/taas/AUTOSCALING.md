# OCR engine autoscaling (HPA / KEDA) — and the GitOps `replicas` gotcha

The in-cluster OCR engine (`ocrEngine` in `values.yaml`) is a **StatefulSet** behind a
headless Service. It can scale three ways via `ocrEngine.autoscaling.mode`:

| mode   | scales by | owns `replicas` |
|--------|-----------|-----------------|
| `none` | nothing — static `ocrEngine.replicas` (default 1) | the manifest |
| `hpa`  | CPU utilisation (`autoscaling/v2` HorizontalPodAutoscaler) | the **HPA** |
| `keda` | Redis queue length (KEDA `ScaledObject`) | **KEDA** |

`minReplicas` / `maxReplicas` bound the autoscaler; `maxReplicas` also caps how many
backend rows taas pre-registers for the engine.

Templates: `templates/ocr-engine.yaml` (StatefulSet + Service),
`templates/ocr-engine-hpa.yaml` (HPA or KEDA ScaledObject).

## The rule: under hpa/keda the chart must NOT declare `spec.replicas`

When an autoscaler owns scaling, the workload manifest must **omit** `spec.replicas`.
The chart does this — `spec.replicas` is rendered **only** for `mode: none`. The
autoscaler's `minReplicas` is the real floor.

### Symptom if this rule is broken

> HPA-spawned pods (`<release>-ocr-engine-1`, `-2`, …) shut down (gracefully drain and
> exit) **right after starting**, and the engine never scales past `minReplicas`.

### Why

If the manifest declares `spec.replicas` (even as a "floor") while an HPA/KEDA also
targets it, the two fight:

1. The autoscaler scales the StatefulSet to N.
2. A reconcile of the manifest — a **GitOps sync** or a `helm upgrade` — sees the
   declared `replicas` (= `minReplicas`) and reverts the live value back to it.
3. The just-created extra pod gets SIGTERM, drains (preStop + `terminationGracePeriodSeconds`),
   and exits. Repeat forever.

Fixed in chart **0.3.1** (commit: omit `spec.replicas` under hpa/keda).

## Argo CD: the chart fix is necessary but NOT sufficient

The chart change stops the manifest from *declaring* `replicas`, but the GitOps
controller must also be told not to *reconcile* that field — otherwise a sync's apply
strips the (now-absent) field and k8s defaults it back to `1`, re-killing scaled pods.

Add this to the taas **Application**:

```yaml
spec:
  syncPolicy:
    syncOptions:
      - RespectIgnoreDifferences=true   # <-- easily-missed, required
  ignoreDifferences:
    - group: apps
      kind: StatefulSet
      name: <release>-ocr-engine        # e.g. taas-ocr-engine
      jsonPointers:
        - /spec/replicas
```

- **`ignoreDifferences`** alone only changes the *diff view*; during an actual **sync**
  Argo still applies desired state. **`RespectIgnoreDifferences=true`** is what makes
  sync leave the ignored field alone.
- Scope by `name` — don't ignore `/spec/replicas` on *all* StatefulSets, since
  redis / minio / CNPG are also StatefulSets whose replicas you *do* want managed.
- Flux equivalent: ignore `/spec/replicas` for the engine StatefulSet via the
  `HelmRelease` drift-detection ignore (or a kustomize patch).
- Plain Helm (no GitOps controller): the chart fix alone is enough.

## Verify

```sh
# StatefulSet name + that the HPA/ScaledObject targets it
kubectl get statefulset,hpa -l app.kubernetes.io/component=ocr-engine -n <ns>

# Rendered manifest must have NO `replicas:` under the ocr-engine StatefulSet (hpa/keda)
helm template <release> deploy/helm/taas --set ocrEngine.enabled=true \
  | awk '/kind: StatefulSet/,/^---/' | grep -E 'name:|replicas:'

# After applying: scaled-up pods should stay Running, not terminate seconds after start
kubectl get pods -l app.kubernetes.io/component=ocr-engine -n <ns> -w
```

## KEDA setup & when to use it

KEDA (queue-depth) is the **recommended production mode** for the OCR engine: a
1-core / `PAGE_WORKERS=1` pod is binary on CPU (idle or pegged on one page), so
CPU-utilisation HPA is a poor signal. Scaling on the Redis `ocr-jobs` queue length
tracks demand directly and scales to zero.

- Requires the **KEDA operator** installed cluster-wide (`hpa` is the default because
  it needs no extra operator).
- `keda.redisAddress` empty => the chart's own Redis (`<release>-redis:<redis.port>`).
  Set it only for an external Redis.
- For a password-protected external Redis, set `keda.redisPasswordSecret` (+ optional
  `keda.redisPasswordKey`) to an existing Secret; the chart renders a
  `TriggerAuthentication` automatically.
- Tune `keda.listLength` (≈1 replica per N queued jobs), `keda.pollingInterval`,
  `keda.cooldownPeriod`.
