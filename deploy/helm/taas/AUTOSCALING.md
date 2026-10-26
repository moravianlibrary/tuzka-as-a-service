# Engine autoscaling (HPA / KEDA) — and the GitOps `replicas` gotcha

Every enabled entry in `engines` (TuzkaOCR, TuzkaM, or any additional engine) is a
**StatefulSet** behind a headless Service. Each one can scale three ways via
`engines.<name>.autoscaling.mode`:

| mode   | scales by | owns `replicas` |
|--------|-----------|-----------------|
| `none` | nothing — static `engines.<name>.autoscaling.replicas` (default 1) | the manifest |
| `hpa`  | CPU utilisation (`autoscaling/v2` HorizontalPodAutoscaler) | the **HPA** |
| `keda` | queued work in seconds from the API's `GET /load` (KEDA `metrics-api` `ScaledObject`) | **KEDA** |

`minReplicas` / `maxReplicas` bound the autoscaler; `maxReplicas` also caps how many
backend rows taas pre-registers for the engine.

Templates: `templates/engine.yaml` (StatefulSet + Service for every enabled engine),
`templates/engine-autoscaler.yaml` (HPA or KEDA ScaledObject per engine).

## The rule: under hpa/keda the chart must NOT declare `spec.replicas`

When an autoscaler owns scaling, the workload manifest must **omit** `spec.replicas`.
The chart does this — `spec.replicas` is rendered **only** for `mode: none`. The
autoscaler's `minReplicas` is the real floor.

### Symptom if this rule is broken

> HPA-spawned pods (`<release>-<engine>-1`, `-2`, …) shut down (gracefully drain and
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
      name: <release>-<engine>          # e.g. taas-ocr-engine or taas-tuzkam
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
kubectl get statefulset,hpa -l taas.io/engine=ocr-engine -n <ns>

# Rendered manifest must have NO `replicas:` under the ocr-engine StatefulSet (hpa/keda)
helm template <release> deploy/helm/taas --set engines.ocr-engine.enabled=true \
  | awk '/kind: StatefulSet/,/^---/' | grep -E 'name:|replicas:'

# After applying: scaled-up pods should stay Running, not terminate seconds after start
kubectl get pods -l taas.io/engine=ocr-engine -n <ns> -w
```

## KEDA setup & when to use it

KEDA (queued work) is the **recommended production mode** for the OCR engine: a
1-core / `PAGE_WORKERS=1` pod is binary on CPU (idle or pegged on one page), so
CPU-utilisation HPA is a poor signal. KEDA scales on what's actually waiting, and can
scale to zero.

The signal is the API's `GET /load`, read by KEDA's `metrics-api` scaler:

```
GET /load?adapter=tuzkaocr        Authorization: Bearer <secrets.loadToken>
{"queued": 120, "inflight": 8, "backlog_s": 1280.0}
```

`backlog_s` is the queued **and in-flight** pages that **this engine could serve** (its
domains, with every output the job asked for) times the measured seconds per page of
each domain
(an average of recent engine run times; `load.default_seconds_per_page`, 10 s, until a
domain has any). KEDA aims for `backlog_s / replicas ≈ keda.targetBacklogSeconds`.
In-flight pages count so that KEDA doesn't scale engines down while they're still
working just because the queue has emptied. It still can when the remaining work
drops, and Kubernetes removes the highest-numbered pod whether or not it's busy;
taas then requeues that pod's pages on another engine (the work is redone, the job
isn't lost).

- Requires the **KEDA operator** installed cluster-wide (`hpa` is the default because
  it needs no extra operator).
- Requires `secrets.loadToken`: the chart puts it in its Secret as `LOAD_TOKEN` and
  renders a `TriggerAuthentication` for it. Rendering fails without it. Without a token,
  `/load` answers 404, so the metric isn't public even though the ingress exposes the
  API.
- `keda.loadFilter` picks the work that counts (`adapter=<engine adapter>` by default;
  `domain=a,b` narrows it to domains).
- Tune `keda.targetBacklogSeconds` (≈1 replica per this many seconds of queued pages),
  `keda.activationBacklogSeconds`, `keda.pollingInterval`, `keda.cooldownPeriod`.
- `group_by=domain|adapter` on `/load` shows the breakdown, for people rather than
  for KEDA.

### Scaling from zero

A pod that isn't running can't tell taas what it serves, so taas remembers: a backend
keeps its last reported domains while it's down, and the register hook **declares**
`engines.<name>.declaredDomains` for each engine before it has ever run (the engine's own
report replaces them on its first sync). That's what makes its queued jobs count in
`/load`, and what lets the API accept them while nothing is running.

**Set `jobs.queued_timeout_seconds` above the engine's cold start** (image pull + model
load). Jobs submitted while the engine is at zero wait in the queue for the first pod;
if the timeout is shorter, they fail before it's ready.
