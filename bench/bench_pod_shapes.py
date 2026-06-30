#!/usr/bin/env python
"""Pod-shape bench: best throughput-per-core for the in-cluster TuzkaOCR CPU engine.

Sweeps engine pod shape × BLAS thread policy, holding PAGE_WORKERS = allocated cores
(page-parallel, OCR_THREADS=1, LINE_WORKERS=1, arena off):

    shapes : 1 core (pw=1) · 2 cores (pw=2) · 4 cores (pw=4)   [cpuset-pinned]
    BLAS   : capped (OMP/OPENBLAS/...=1)  vs  uncapped (=cores)

The question: is it more efficient (pages/sec per CPU core) to deploy many small
page-parallel pods or fewer larger ones, and does letting numpy/opencv use BLAS
threads on top of page-parallelism help or hurt? The winner is the shape with the
highest pages/sec PER CORE — that's what you replicate to fill a cluster CPU budget.

Driven through the live docker-compose stack (engine env parameterized via $BENCH_*).
Backend 1 max_inflight is bumped to 8 for the run (dispatch headroom so even pw=4 is
never starved) and restored to 4 after. Per-job timing read from Postgres.

Rounds are interleaved (config order rotated per round); medians reported.
Writes bench/RESULTS_POD_SHAPES.md + bench/pod_shapes_raw.tsv.

Does NOT restore docker-compose.yml — afterward run
`git checkout -- docker-compose.yml && docker compose up -d ocr-engine`.

Usage: venv/bin/python bench/bench_pod_shapes.py [--n 40] [--rounds 3]
"""
from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

PARENT = Path(__file__).resolve().parent.parent
IMAGES_DIR = PARENT / "test-data"
RESULTS_MD = PARENT / "bench" / "RESULTS_POD_SHAPES.md"
RAW_TSV = PARENT / "bench" / "pod_shapes_raw.tsv"

TAAS_URL = os.environ.get("TAAS_URL", "http://localhost:8080")
MASTER_KEY = os.environ.get("MASTER_KEY", "test-master-key")
FMT = "txt"
SUBMIT_CONCURRENCY = 8
DRAIN_TIMEOUT_S = 1200
ENGINE_SVC = "ocr-engine"
PG_SVC = "postgres"
BACKEND_ID = 1
BENCH_MAX_INFLIGHT = 8

# (cores, cpuset). PAGE_WORKERS is set = cores. cpusets are disjoint-friendly slices.
SHAPES = [(1, "0"), (2, "0,1"), (4, "0,1,2,3")]
# (label, blas_threads | None->cores)
BLAS_MODES = [("capped", 1), ("uncapped", None)]


def sh(cmd: list[str], env: dict | None = None, check: bool = True) -> str:
    full_env = {**os.environ, **(env or {})}
    r = subprocess.run(cmd, cwd=str(PARENT), capture_output=True, text=True, env=full_env)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed ({r.returncode}): {' '.join(cmd)}\n{r.stderr}")
    return r.stdout.strip()


def psql(sql: str) -> str:
    return sh(["docker", "compose", "exec", "-T", PG_SVC,
               "psql", "-U", "taas", "-d", "taas", "-tAc", sql])


def engine_cid() -> str:
    return sh(["docker", "compose", "ps", "-q", ENGINE_SVC])


def recreate_engine(cpuset: str, pw: int, blas: int) -> None:
    print(f"  recreating engine (cpuset={cpuset} pw={pw} blas={blas}) ...", flush=True)
    sh(["docker", "compose", "up", "-d", "--no-deps", ENGINE_SVC],
       env={"BENCH_CPUSET": cpuset, "BENCH_PAGE_WORKERS": str(pw), "BENCH_BLAS": str(blas)})
    deadline = time.time() + 180
    probe = ("import urllib.request;"
             "urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5);print('ok')")
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "compose", "exec", "-T", ENGINE_SVC, "python", "-c", probe],
            cwd=str(PARENT), capture_output=True, text=True)
        if r.returncode == 0 and "ok" in r.stdout:
            time.sleep(3)
            print("  engine healthy.", flush=True)
            return
        time.sleep(2)
    raise RuntimeError("engine did not become healthy within 180s")


class RssSampler(threading.Thread):
    def __init__(self, cid: str):
        super().__init__(daemon=True)
        self._cid = cid
        self._stop = threading.Event()
        self.peak_mib = 0.0

    @staticmethod
    def _to_mib(s: str) -> float:
        s = s.strip()
        num = float("".join(c for c in s if (c.isdigit() or c == ".")) or 0)
        u = s.lower()
        if "gib" in u or "gb" in u:
            return num * 1024
        if "kib" in u or "kb" in u:
            return num / 1024
        return num

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", self._cid],
                    capture_output=True, text=True, timeout=10)
                used = out.stdout.split("/")[0]
                if used.strip():
                    self.peak_mib = max(self.peak_mib, self._to_mib(used))
            except Exception:
                pass
            self._stop.wait(2.0)

    def stop(self) -> float:
        self._stop.set()
        self.join(timeout=5)
        return self.peak_mib


def make_bench_user(http: httpx.Client) -> tuple[str, str]:
    username = f"shapebench-{uuid.uuid4().hex[:8]}"
    r = http.post(f"{TAAS_URL}/admin/users",
                  headers={"X-Master-Key": MASTER_KEY}, json={"username": username})
    r.raise_for_status()
    api_key = r.json()["api_key"]
    http.patch(f"{TAAS_URL}/admin/users/{username}",
               headers={"X-Master-Key": MASTER_KEY},
               json={"rate_submit_per_minute": 1_000_000, "burst_submit": 100_000,
                     "rate_query_per_minute": 1_000_000, "burst_query": 100_000}
               ).raise_for_status()
    print(f"  bench user: {username}", flush=True)
    return username, api_key


def set_backend_inflight(http: httpx.Client, value: int) -> None:
    http.patch(f"{TAAS_URL}/admin/backends/{BACKEND_ID}",
               headers={"X-Master-Key": MASTER_KEY},
               json={"max_inflight": value}).raise_for_status()
    print(f"  backend {BACKEND_ID} max_inflight -> {value}", flush=True)


def submit_one(api_key: str, img: Path) -> int:
    with httpx.Client(timeout=60.0) as c:
        r = c.post(f"{TAAS_URL}/api/v1/jobs", headers={"X-API-Key": api_key},
                   files={"image": (img.name, img.read_bytes())},
                   data={"uuid": str(uuid.uuid4()), "fmt": FMT})
        return r.status_code


def submit_batch(api_key: str, images: list[Path]) -> None:
    bad = 0
    with ThreadPoolExecutor(max_workers=SUBMIT_CONCURRENCY) as ex:
        for f in as_completed([ex.submit(submit_one, api_key, img) for img in images]):
            if f.result() >= 400:
                bad += 1
    if bad:
        print(f"  WARNING: {bad}/{len(images)} submits returned >=400", flush=True)


def drain(username: str, run_start: str, n: int) -> None:
    deadline = time.time() + DRAIN_TIMEOUT_S
    done = 0
    while time.time() < deadline:
        done = int(psql(
            f"SELECT count(*) FROM jobs WHERE username='{username}' "
            f"AND submitted_at >= '{run_start}' AND status IN ('done','failed');"))
        if done >= n:
            return
        time.sleep(2)
    raise RuntimeError(f"drain timeout: only {done}/{n} finished in {DRAIN_TIMEOUT_S}s")


def run_once(username: str, api_key: str, images: list[Path],
             cores: int, cpuset: str, blas: int) -> dict:
    recreate_engine(cpuset, cores, blas)
    run_start = psql("SELECT now();")
    sampler = RssSampler(engine_cid())
    sampler.start()
    submit_batch(api_key, images)
    drain(username, run_start, len(images))
    peak_mib = sampler.stop()

    rows = psql(
        "SELECT status, requeues, "
        "EXTRACT(EPOCH FROM (finished_at - started_at)), "
        "EXTRACT(EPOCH FROM started_at), EXTRACT(EPOCH FROM finished_at) "
        f"FROM jobs WHERE username='{username}' AND submitted_at >= '{run_start}';")

    run_s, starts, finishes = [], [], []
    failed = requeues = 0
    for line in rows.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        status, rq, rs, st, fn = parts[:5]
        requeues += int(rq or 0)
        if status == "failed":
            failed += 1
            continue
        if rs:
            run_s.append(float(rs))
        if st:
            starts.append(float(st))
        if fn:
            finishes.append(float(fn))

    wall = (max(finishes) - min(starts)) if (finishes and starts) else 0.0
    thr = (len(run_s) / wall) if wall > 0 else 0.0
    return {"cores": cores, "blas": blas, "run_s": run_s, "throughput": thr,
            "thr_per_core": thr / cores, "wall": wall, "done": len(run_s),
            "failed": failed, "requeues": requeues, "peak_mib": peak_mib}


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def write_report(results: dict[str, list[dict]], order: list[str], n: int, rounds: int) -> None:
    lines = [
        "# Bench — TuzkaOCR pod shape × BLAS policy (throughput per core)\n",
        f"`PAGE_WORKERS = cores` (page-parallel), `OCR_THREADS=1 LINE_WORKERS=1 "
        f"CPU_MEM_ARENA=false`, cpuset-pinned, backend `max_inflight=8`, engine "
        f"`MAX_QUEUE=8`. {n} pages/run · {rounds} interleaved rounds · medians.\n",
        "**Goal: highest pages/sec PER CORE = best pod shape to replicate across a "
        "cluster CPU budget.**\n",
        "| shape | BLAS | pg/s (median) | **pg/s/core** | p95 run s | peak RSS MiB | "
        "done | failed | requeues |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key in order:
        runs = results[key]
        cores = runs[0]["cores"]
        blas = key.split("-", 1)[1]   # from the config key; on 1 core capped==uncapped
        thr = statistics.median([r["throughput"] for r in runs])
        tpc = statistics.median([r["thr_per_core"] for r in runs])
        p95 = pct([x for r in runs for x in r["run_s"]], 0.95)
        peak = max(r["peak_mib"] for r in runs)
        done = sum(r["done"] for r in runs)
        failed = sum(r["failed"] for r in runs)
        rq = sum(r["requeues"] for r in runs)
        lines.append(f"| {cores}-core | {blas} | {thr:.3f} | **{tpc:.3f}** | {p95:.1f} | "
                     f"{peak:.0f} | {done} | {failed} | {rq} |")

    lines.append("\n## Per-round raw\n")
    lines.append("| config | round | done | failed | wall s | pg/s | pg/s/core | peak MiB |")
    lines.append("|---|---|---|---|---|---|---|---|")
    with RAW_TSV.open("w") as tsv:
        tsv.write("config\tround\tcores\tblas\tdone\tfailed\trequeues\twall_s\t"
                  "throughput\tthr_per_core\tpeak_mib\n")
        for key in order:
            for i, r in enumerate(results[key], 1):
                lines.append(f"| {key} | {i} | {r['done']} | {r['failed']} | {r['wall']:.1f} | "
                             f"{r['throughput']:.3f} | {r['thr_per_core']:.3f} | {r['peak_mib']:.0f} |")
                tsv.write(f"{key}\t{i}\t{r['cores']}\t{r['blas']}\t{r['done']}\t{r['failed']}\t"
                          f"{r['requeues']}\t{r['wall']:.1f}\t{r['throughput']:.3f}\t"
                          f"{r['thr_per_core']:.3f}\t{r['peak_mib']:.0f}\n")

    RESULTS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS_MD}\nwrote {RAW_TSV}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    images = sorted(IMAGES_DIR.glob("*_image.jpeg"))[: args.n]
    if len(images) < args.n:
        raise SystemExit(f"only {len(images)} images, need {args.n}")

    # config list: (key, cores, cpuset, blas_threads)
    configs = []
    for cores, cpuset in SHAPES:
        for label, bt in BLAS_MODES:
            configs.append((f"{cores}c-{label}", cores, cpuset, bt if bt is not None else cores))
    order = [c[0] for c in configs]
    print(f"corpus: {len(images)} pages · configs={order} · rounds={args.rounds}", flush=True)

    http = httpx.Client(timeout=60.0)
    username, api_key = make_bench_user(http)
    set_backend_inflight(http, BENCH_MAX_INFLIGHT)
    results: dict[str, list[dict]] = {c[0]: [] for c in configs}
    try:
        for r in range(args.rounds):
            rot = configs[r % len(configs):] + configs[: r % len(configs)]
            print(f"\n=== round {r + 1}/{args.rounds} ===", flush=True)
            for key, cores, cpuset, blas in rot:
                res = run_once(username, api_key, images, cores, cpuset, blas)
                results[key].append(res)
                print(f"  [{key}] thr={res['throughput']:.3f} pg/s  "
                      f"per-core={res['thr_per_core']:.3f}  done={res['done']} "
                      f"failed={res['failed']} rq={res['requeues']} "
                      f"peak={res['peak_mib']:.0f}MiB", flush=True)
    finally:
        set_backend_inflight(http, 4)   # restore registered value
        http.close()

    write_report(results, order, args.n, args.rounds)


if __name__ == "__main__":
    main()
