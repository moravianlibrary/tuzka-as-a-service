#!/usr/bin/env python
"""Bench TUZKAOCR_PAGE_WORKERS ∈ {1,2,3} through the live docker-compose stack.

Engine env is held to deploy/helm/taas/values.yaml (OCR_THREADS=1, LINE_WORKERS=1,
MAX_QUEUE=4, CPU_MEM_ARENA=false) and pinned to 2 cores via `cpuset: "0,1"` in
docker-compose.yml; only PAGE_WORKERS is swept (passed as $BENCH_PAGE_WORKERS).

For each (round, page_workers) it recreates the engine, fires N pages at the taas
API fast enough to saturate the engine queue, waits for them to drain, then reads
per-job timing from Postgres:

    running time = finished_at - started_at   (the "time in running" we care about)
    queue wait   = started_at - submitted_at  (taas-side wait before dispatch)
    throughput   = jobs_done / (max(finished_at) - min(started_at))   pages/sec

Rounds are interleaved (pw order rotated per round) to control thermal/order drift;
medians are reported. Writes bench/RESULTS_PAGE_WORKERS.md + bench/page_workers_raw.tsv.

The harness does NOT restore docker-compose.yml — do that afterward with
`git checkout -- docker-compose.yml && docker compose up -d ocr-engine`.

Usage: venv/bin/python bench/bench_page_workers.py [--n 60] [--rounds 3] [--pws 1,2,3]
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
RESULTS_MD = PARENT / "bench" / "RESULTS_PAGE_WORKERS.md"
RAW_TSV = PARENT / "bench" / "page_workers_raw.tsv"

TAAS_URL = os.environ.get("TAAS_URL", "http://localhost:8080")
MASTER_KEY = os.environ.get("MASTER_KEY", "test-master-key")
FMT = "txt"                      # lightest downstream I/O; engine cost dominates
SUBMIT_CONCURRENCY = 8
DRAIN_TIMEOUT_S = 900
ENGINE_SVC = "ocr-engine"
PG_SVC = "postgres"


# --------------------------------------------------------------------------- #
# shell helpers
# --------------------------------------------------------------------------- #
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


def recreate_engine(pw: int) -> None:
    """Recreate only the engine with PAGE_WORKERS=pw, then wait until it serves /healthz."""
    print(f"  recreating engine (PAGE_WORKERS={pw}) ...", flush=True)
    sh(["docker", "compose", "up", "-d", "--no-deps", ENGINE_SVC],
       env={"BENCH_PAGE_WORKERS": str(pw)})
    # The compose healthcheck has a slow cadence (60s start_period); poll directly.
    deadline = time.time() + 180
    probe = ("import urllib.request,sys;"
             "urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5);"
             "print('ok')")
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "compose", "exec", "-T", ENGINE_SVC, "python", "-c", probe],
            cwd=str(PARENT), capture_output=True, text=True)
        if r.returncode == 0 and "ok" in r.stdout:
            time.sleep(3)  # small settle so taas re-marks the backend reachable
            print("  engine healthy.", flush=True)
            return
        time.sleep(2)
    raise RuntimeError("engine did not become healthy within 180s")


# --------------------------------------------------------------------------- #
# RSS sampler (peak engine memory during a run)
# --------------------------------------------------------------------------- #
class RssSampler(threading.Thread):
    def __init__(self, cid: str):
        super().__init__(daemon=True)
        self._cid = cid
        self._stop = threading.Event()
        self.peak_mib = 0.0

    @staticmethod
    def _to_mib(s: str) -> float:
        s = s.strip()
        num = float("".join(c for c in s if (c.isdigit() or c == ".")))
        u = s.lower()
        if "gib" in u or "gb" in u:
            return num * 1024
        if "kib" in u or "kb" in u:
            return num / 1024
        return num  # MiB/MB

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


# --------------------------------------------------------------------------- #
# taas user / submission
# --------------------------------------------------------------------------- #
def make_bench_user(http: httpx.Client) -> tuple[str, str]:
    username = f"bench-{uuid.uuid4().hex[:8]}"
    r = http.post(f"{TAAS_URL}/admin/users",
                  headers={"X-Master-Key": MASTER_KEY},
                  json={"username": username})
    r.raise_for_status()
    api_key = r.json()["api_key"]
    # Lift rate limits so 60 rapid submits aren't throttled (default 60/min, burst 10).
    http.patch(f"{TAAS_URL}/admin/users/{username}",
               headers={"X-Master-Key": MASTER_KEY},
               json={"rate_submit_per_minute": 1_000_000, "burst_submit": 100_000,
                     "rate_query_per_minute": 1_000_000, "burst_query": 100_000}
               ).raise_for_status()
    print(f"  bench user: {username}", flush=True)
    return username, api_key


def submit_one(api_key: str, img: Path) -> int:
    with httpx.Client(timeout=60.0) as c:
        r = c.post(f"{TAAS_URL}/api/v1/jobs",
                   headers={"X-API-Key": api_key},
                   files={"image": (img.name, img.read_bytes())},
                   data={"uuid": str(uuid.uuid4()), "fmt": FMT})
        return r.status_code


def submit_batch(api_key: str, images: list[Path]) -> None:
    bad = 0
    with ThreadPoolExecutor(max_workers=SUBMIT_CONCURRENCY) as ex:
        futs = [ex.submit(submit_one, api_key, img) for img in images]
        for f in as_completed(futs):
            if f.result() >= 400:
                bad += 1
    if bad:
        print(f"  WARNING: {bad}/{len(images)} submits returned >=400", flush=True)


def drain(username: str, run_start: str, n: int) -> None:
    deadline = time.time() + DRAIN_TIMEOUT_S
    while time.time() < deadline:
        done = int(psql(
            f"SELECT count(*) FROM jobs WHERE username='{username}' "
            f"AND submitted_at >= '{run_start}' AND status IN ('done','failed');"))
        if done >= n:
            return
        time.sleep(2)
    raise RuntimeError(f"drain timeout: only {done}/{n} finished in {DRAIN_TIMEOUT_S}s")


# --------------------------------------------------------------------------- #
# one run = one (round, pw): recreate engine, submit, drain, read timings
# --------------------------------------------------------------------------- #
def run_once(username: str, api_key: str, images: list[Path], pw: int) -> dict:
    recreate_engine(pw)
    run_start = psql("SELECT now();")
    sampler = RssSampler(engine_cid())
    sampler.start()
    submit_batch(api_key, images)
    drain(username, run_start, len(images))
    peak_mib = sampler.stop()

    rows = psql(
        "SELECT status, requeues, "
        "EXTRACT(EPOCH FROM (finished_at - started_at)), "
        "EXTRACT(EPOCH FROM (started_at - submitted_at)), "
        "EXTRACT(EPOCH FROM started_at), EXTRACT(EPOCH FROM finished_at) "
        f"FROM jobs WHERE username='{username}' AND submitted_at >= '{run_start}';")

    run_s, queue_s, starts, finishes = [], [], [], []
    failed = requeues = 0
    for line in rows.splitlines():
        parts = line.split("|")
        if len(parts) < 6:
            continue
        status, rq, rs, qs, st, fn = parts[:6]
        requeues += int(rq or 0)
        if status == "failed":
            failed += 1
            continue
        if rs:
            run_s.append(float(rs))
        if qs:
            queue_s.append(float(qs))
        if st:
            starts.append(float(st))
        if fn:
            finishes.append(float(fn))

    wall = (max(finishes) - min(starts)) if (finishes and starts) else 0.0
    throughput = (len(run_s) / wall) if wall > 0 else 0.0
    return {
        "pw": pw, "run_s": run_s, "queue_s": queue_s,
        "throughput": throughput, "wall": wall,
        "done": len(run_s), "failed": failed, "requeues": requeues,
        "peak_mib": peak_mib,
    }


# --------------------------------------------------------------------------- #
# stats + report
# --------------------------------------------------------------------------- #
def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def write_report(per_pw: dict[int, list[dict]], n: int, rounds: int) -> None:
    pws = sorted(per_pw)
    lines = [
        "# Bench — TUZKAOCR_PAGE_WORKERS sweep (live stack, 2-core engine)\n",
        f"Engine pinned to 2 cores (`cpuset: \"0,1\"`), env aligned to `values.yaml` "
        f"(`OCR_THREADS=1 LINE_WORKERS=1 MAX_QUEUE=4 CPU_MEM_ARENA=false`). "
        f"Backend `max_inflight=4`. {n} pages/run · {rounds} interleaved rounds · "
        f"medians across rounds. `fmt={FMT}`.\n",
        "## Running time  (`finished_at − started_at`, seconds/page)\n",
        "| page_workers | median | p95 | max |",
        "|---|---|---|---|",
    ]
    for pw in pws:
        all_run = [x for r in per_pw[pw] for x in r["run_s"]]
        lines.append(f"| {pw} | {statistics.median(all_run):.2f} | "
                     f"{pct(all_run, 0.95):.2f} | {max(all_run):.2f} |")

    lines += ["\n## Throughput (pages/sec, queue saturated)  +  health\n",
              "| page_workers | throughput (median) | taas queue wait (median s) | "
              "done | failed | requeues | engine peak RSS (MiB) |",
              "|---|---|---|---|---|---|---|"]
    for pw in pws:
        runs = per_pw[pw]
        tps = statistics.median([r["throughput"] for r in runs])
        qwait = statistics.median([x for r in runs for x in r["queue_s"]] or [0])
        done = sum(r["done"] for r in runs)
        failed = sum(r["failed"] for r in runs)
        requeues = sum(r["requeues"] for r in runs)
        peak = max(r["peak_mib"] for r in runs)
        lines.append(f"| {pw} | {tps:.3f} | {qwait:.2f} | {done} | {failed} | "
                     f"{requeues} | {peak:.0f} |")

    lines.append("\n## Per-round raw\n")
    lines.append("| round-pw | done | failed | wall (s) | throughput | median run s | peak MiB |")
    lines.append("|---|---|---|---|---|---|---|")
    with RAW_TSV.open("w") as tsv:
        tsv.write("pw\tround\tdone\tfailed\trequeues\twall_s\tthroughput\tmedian_run_s\tpeak_mib\n")
        for pw in pws:
            for i, r in enumerate(per_pw[pw], 1):
                med = statistics.median(r["run_s"]) if r["run_s"] else 0.0
                lines.append(f"| r{i}-pw{pw} | {r['done']} | {r['failed']} | {r['wall']:.1f} | "
                             f"{r['throughput']:.3f} | {med:.2f} | {r['peak_mib']:.0f} |")
                tsv.write(f"{pw}\t{i}\t{r['done']}\t{r['failed']}\t{r['requeues']}\t"
                          f"{r['wall']:.1f}\t{r['throughput']:.3f}\t{med:.2f}\t{r['peak_mib']:.0f}\n")

    RESULTS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS_MD}\nwrote {RAW_TSV}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--pws", default="1,2,3")
    args = ap.parse_args()
    pws = [int(x) for x in args.pws.split(",")]

    images = sorted(IMAGES_DIR.glob("*_image.jpeg"))[: args.n]
    if len(images) < args.n:
        raise SystemExit(f"only {len(images)} images found, need {args.n}")
    print(f"corpus: {len(images)} pages · pws={pws} · rounds={args.rounds}", flush=True)

    http = httpx.Client(timeout=60.0)
    username, api_key = make_bench_user(http)

    per_pw: dict[int, list[dict]] = {pw: [] for pw in pws}
    for r in range(args.rounds):
        order = pws[r % len(pws):] + pws[: r % len(pws)]   # rotate per round
        print(f"\n=== round {r + 1}/{args.rounds}  order={order} ===", flush=True)
        for pw in order:
            res = run_once(username, api_key, images, pw)
            per_pw[pw].append(res)
            print(f"  [pw={pw}] done={res['done']} failed={res['failed']} "
                  f"requeues={res['requeues']} thr={res['throughput']:.3f} pg/s "
                  f"median_run={statistics.median(res['run_s']) if res['run_s'] else 0:.2f}s "
                  f"peak={res['peak_mib']:.0f}MiB", flush=True)

    write_report(per_pw, args.n, args.rounds)
    http.close()


if __name__ == "__main__":
    main()
