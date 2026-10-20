"""Shared plumbing for the legacy-vs-new API benchmarks.

Both `legacy_proarc.py` (PERO-style API via compat, as ProArc drives it) and
`new_client.py` (taas API via `taas_client`) measure the *same* corpus and emit the
*same* summary shape, so `run_scenarios.py` can sweep them and tabulate the results.

Kept dependency-light on purpose: httpx only, no pytest/pandas.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

# Everything ProArc's PeroOcrProcessor.getContentType() knows about, plus png.
# .jp2 is NOT in taas's default ALLOWED_EXTENSIONS — see preflight_extensions().
IMAGE_EXTENSIONS = (".jp2", ".jpf", ".j2k", ".jpx", ".tif", ".tiff", ".jpg", ".jpeg", ".png")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_TAAS_URL = "http://localhost:8080"
DEFAULT_COMPAT_URL = "http://localhost:8001"
DEFAULT_IMAGE_DIR = REPO_ROOT / "test-data"

# Effectively "no limit" for a bench user. See provision_user() for why this is a big
# number rather than 0.
UNTHROTTLED_PER_MINUTE = 100_000


# --- Corpus -----------------------------------------------------------------


def discover_images(
    directory: Path, limit: int | None = None, seed: int | None = None
) -> list[Path]:
    """Return image paths under `directory`, capped at `limit`.

    Without a seed this is the first `limit` paths in sorted order — stable, but it
    means every run benches the same pages, so a quirk of that particular slice
    (unusually dense scans, say) silently colours every result.

    With a seed the slice is drawn at random from the whole pool instead. The seed
    is chosen **once per sweep and shared by every case**, never per case: page
    density drives OCR time hard, so two arms drawing different samples would fold
    corpus variance straight into the comparison. Same seed -> same pages -> the
    comparison stays paired, and a run reproduces exactly when the seed is reused.
    """
    if not directory.is_dir():
        raise SystemExit(f"image dir not found: {directory}")

    images = sorted(p for p in directory.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS)
    if not images:
        raise SystemExit(f"no images in {directory} (looked for {', '.join(IMAGE_EXTENSIONS)})")

    if not limit or limit >= len(images):
        return images

    if seed is None:
        return images[:limit]

    return sorted(random.Random(seed).sample(images, limit))


def preflight_extensions(images: Sequence[Path]) -> None:
    """Warn loudly about extensions taas rejects out of the box.

    taas gates uploads on `ALLOWED_EXTENSIONS`, whose default omits JPEG 2000 even
    though ProArc's legacy client explicitly supports it. Better to say so up front
    than to watch every page fail with a 400.
    """
    exts = {p.suffix.lower() for p in images}
    jpeg2000 = exts & {".jp2", ".jpf", ".j2k", ".jpx"}
    if not jpeg2000:
        return

    print(
        f"  !! corpus contains {'/'.join(sorted(jpeg2000))} — taas's default\n"
        f"     ALLOWED_EXTENSIONS is [.tif,.tiff,.jpg,.jpeg,.png], so these upload as 400.\n"
        f"     Add them in .env.app and restart the api:\n"
        f'       ALLOWED_EXTENSIONS=[".tif",".tiff",".jpg",".jpeg",".png",".jp2"]\n'
    )


# --- Artifacts --------------------------------------------------------------


class MissingArtifact(Exception):
    """A page came back without both formats, so this run isn't comparable."""


def store_artifacts(out_dir: Path | None, page_name: str, alto: bytes, txt: bytes) -> None:
    """Write the decompressed ALTO + text pair, exactly as ProArc leaves them on disk.

    Both clients must finish holding *both* uncompressed artifacts, or one side is
    doing less work than the other and the comparison is meaningless — so a page
    missing either format is an error, not a warning. ProArc writes `<name>.xml` and
    `<name>.txt` next to the image; we mirror that under `out_dir`.

    The write is inside the timed region on purpose: it's real work both sides do.
    """
    if not alto:
        raise MissingArtifact(f"{page_name}: no ALTO returned")
    if not txt:
        raise MissingArtifact(f"{page_name}: no text returned")

    if out_dir is None:
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(page_name).stem
    (out_dir / f"{stem}.xml").write_bytes(alto)
    (out_dir / f"{stem}.txt").write_bytes(txt)


# --- User provisioning ------------------------------------------------------


_TAAS_ADMIN_SH = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "taas-admin.sh"


def taas_admin(*args: str, env: dict[str, str] | None = None) -> str:
    """Run ``taas-admin`` in the api container and return its stdout.

    Goes through ``scripts/lib/taas-admin.sh``, so ``TAAS_ADMIN_MODE=compose|k3d`` picks
    the stack (run_scenarios.py sets it from ``--target``). Secrets go in ``env`` and are
    named in ``TAAS_ADMIN_ENV``, never in argv.
    """
    full_env = {**os.environ, **(env or {})}
    if env:
        full_env["TAAS_ADMIN_ENV"] = " ".join(env)
    result = subprocess.run(
        ["bash", "-c", f'source "{_TAAS_ADMIN_SH}" && taas_admin "$@"', "taas_admin", *args],
        env=full_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"taas-admin {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def provision_user(prefix: str, *, lift_rate_limits: bool = True) -> str:
    """Create a throwaway taas user and return its raw API key.

    The key doubles as the legacy `api-key`, since compat just forwards it as
    `X-API-Key`. Rate limits are lifted by default so the benchmark measures the OCR
    pipeline rather than the GCRA limiter — pass `lift_rate_limits=False` to keep the
    stock limits (which is the interesting run if you want to see legacy polling burn
    a user's query budget).
    """
    username = f"{prefix}-{int(time.time())}"
    api_key = taas_admin("user", "create", username).strip()

    if lift_rate_limits:
        # NOT zero: app.services.rate_limit.check() treats per_minute <= 0 as
        # "deny everything" (a misconfigured limit must fail closed), so zero
        # would 429 the whole run. A large finite rate is the way to opt out,
        # and burst has to follow it or GCRA still gates the initial spike.
        limit = str(UNTHROTTLED_PER_MINUTE)
        taas_admin(
            "user",
            "set-limits",
            username,
            *("--submit-per-minute", limit, "--submit-burst", limit),
            *("--query-per-minute", limit, "--query-burst", limit),
            *("--ws-per-minute", limit, "--ws-burst", limit),
        )

    print(f"  user={username} (rate limits {'lifted' if lift_rate_limits else 'stock'})")
    return api_key


# --- Server-side timing breakdown -------------------------------------------

JOBS_PAGE_LIMIT = 200


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None

    return datetime.fromisoformat(raw)


def fetch_server_timings(taas_url: str, api_key: str) -> dict[str, float | int]:
    """Pull taas's own per-job timestamps to separate engine time from plumbing.

    taas records `submitted_at` -> `started_at` -> `finished_at` per job, so the OCR
    engine's own working window can be subtracted from what the client observed:

        engine   = finished_at - started_at     (the engine actually working)
        queue    = started_at  - submitted_at   (dispatch + waiting for a free slot)
        server   = finished_at - submitted_at   (everything taas owns)
        overhead = observed latency - server    (the client-side protocol cost:
                   open/upload round trips, poll quantisation, result download)

    Only *durations* are compared, never a server clock against the client's
    monotonic one, so clock skew between the two cannot distort this.

    Every case runs as a fresh user, so all of that user's jobs belong to this run —
    which is what makes this work for the legacy side too, where compat mints its own
    `external_id` and the client never learns the taas job id. The figures are
    therefore **aggregates over the run**, not per-page.
    """
    engine: list[float] = []
    queue: list[float] = []
    server: list[float] = []
    offset = 0

    with httpx.Client(base_url=taas_url, timeout=30.0) as http:
        while True:
            resp = http.get(
                "/api/v1/jobs",
                headers={"X-API-Key": api_key},
                params={"limit": JOBS_PAGE_LIMIT, "offset": offset},
            )
            if resp.status_code != 200:
                return {}

            payload = resp.json()
            jobs = payload.get("jobs", [])

            for job in jobs:
                submitted = _parse_ts(job.get("submitted_at"))
                started = _parse_ts(job.get("started_at"))
                finished = _parse_ts(job.get("finished_at"))

                if started and finished:
                    engine.append((finished - started).total_seconds())
                if submitted and started:
                    queue.append((started - submitted).total_seconds())
                if submitted and finished:
                    server.append((finished - submitted).total_seconds())

            offset += len(jobs)
            if len(jobs) < JOBS_PAGE_LIMIT or offset >= payload.get("total", 0):
                break

    if not server:
        return {}

    return {
        "jobs_measured": len(server),
        "engine_mean": round(statistics.fmean(engine), 2) if engine else 0.0,
        "engine_total": round(sum(engine), 2),
        "queue_mean": round(statistics.fmean(queue), 2) if queue else 0.0,
        "server_mean": round(statistics.fmean(server), 2),
    }


# --- Timings ----------------------------------------------------------------


@dataclass
class PageTiming:
    """One page's end-to-end trip through whichever API is under test."""

    name: str
    started: float
    finished: float
    ok: bool
    http_requests: int = 0
    polls: int = 0
    alto_bytes: int = 0
    txt_bytes: int = 0
    error: str | None = None

    @property
    def seconds(self) -> float:
        return self.finished - self.started


@dataclass
class BenchRun:
    """Everything one benchmark run produced, ready to summarize or serialize."""

    mode: str
    concurrency: int
    wall_seconds: float
    timings: list[PageTiming] = field(default_factory=list)
    notes: dict[str, object] = field(default_factory=dict)
    server: dict[str, float | int] = field(default_factory=dict)


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile — no interpolation, no numpy."""
    if not values:
        return 0.0

    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(pct / 100 * len(ordered)))))
    return ordered[rank - 1]


def summarize(run: BenchRun) -> dict[str, object]:
    """Reduce a run to the comparable numbers `run_scenarios` tabulates."""
    done = [t for t in run.timings if t.ok]
    failed = [t for t in run.timings if not t.ok]
    latencies = [t.seconds for t in done]

    return {
        "mode": run.mode,
        "concurrency": run.concurrency,
        "pages": len(run.timings),
        "ok": len(done),
        "failed": len(failed),
        "wall_seconds": round(run.wall_seconds, 2),
        "pages_per_minute": round(len(done) / run.wall_seconds * 60, 2) if run.wall_seconds else 0,
        "latency_mean": round(statistics.fmean(latencies), 2) if latencies else 0,
        "latency_p50": round(percentile(latencies, 50), 2),
        "latency_p95": round(percentile(latencies, 95), 2),
        "latency_max": round(max(latencies), 2) if latencies else 0,
        "http_requests": sum(t.http_requests for t in run.timings),
        "polls": sum(t.polls for t in run.timings),
        "alto_bytes": sum(t.alto_bytes for t in run.timings),
        "txt_bytes": sum(t.txt_bytes for t in run.timings),
        "errors": sorted({t.error for t in failed if t.error})[:5],
        "notes": run.notes,
        **_overhead(run, latencies),
    }


def _overhead(run: BenchRun, latencies: list[float]) -> dict[str, object]:
    """Client-side protocol cost: observed latency minus taas's own submitted->finished."""
    if not run.server or not latencies:
        return {}

    latency_mean = statistics.fmean(latencies)
    server_mean = float(run.server["server_mean"])

    return {
        "engine_mean": run.server["engine_mean"],
        "queue_mean": run.server["queue_mean"],
        "server_mean": run.server["server_mean"],
        # Can go slightly negative on a fast local link, since the server window starts
        # at submitted_at — i.e. after the client's upload has already landed.
        "overhead_mean": round(latency_mean - server_mean, 2),
        "jobs_measured": run.server["jobs_measured"],
    }


def print_report(summary: dict[str, object]) -> None:
    """Print one run's summary as an aligned key/value block."""
    print(f"\n=== {summary['mode']} ===")
    for key in (
        "pages",
        "ok",
        "failed",
        "concurrency",
        "wall_seconds",
        "pages_per_minute",
        "latency_mean",
        "latency_p50",
        "latency_p95",
        "latency_max",
        "http_requests",
        "polls",
        "alto_bytes",
        "txt_bytes",
    ):
        print(f"  {key:<18} {summary[key]}")

    # Present only when taas's job timestamps could be read.
    for key in ("engine_mean", "queue_mean", "server_mean", "overhead_mean", "jobs_measured"):
        if key in summary:
            print(f"  {key:<18} {summary[key]}")

    errors = summary.get("errors")
    if errors:
        print(f"  {'errors':<18} {errors}")


def write_json(path: Path | None, summary: dict[str, object], run: BenchRun) -> None:
    """Persist the summary (plus raw per-page timings) for later comparison."""
    if not path:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**summary, "timings": [asdict(t) for t in run.timings]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  wrote {path}")


# --- CLI --------------------------------------------------------------------


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Register the flags both benchmarks share."""
    parser.add_argument("--images", type=Path, default=DEFAULT_IMAGE_DIR, help="image directory")
    parser.add_argument("--n", type=int, default=10, help="how many pages to run")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="draw the N pages at random from the corpus (same seed -> same pages)",
    )
    parser.add_argument("--taas-url", default=DEFAULT_TAAS_URL)
    parser.add_argument("--api-key", default=None, help="reuse a user key instead of creating one")
    parser.add_argument(
        "--stock-limits",
        action="store_true",
        help="keep the user's default rate limits (do not lift them for the run)",
    )
    parser.add_argument("--timeout", type=float, default=1800.0, help="per-page timeout (s)")
    parser.add_argument("--json", type=Path, default=None, help="write the summary here")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="write each page's decompressed .xml + .txt here (a <mode>/ subdir is added)",
    )


def resolve_api_key(args: argparse.Namespace, prefix: str) -> str:
    """Use the supplied key, else mint a fresh user."""
    if args.api_key:
        return str(args.api_key)

    return provision_user(prefix, lift_rate_limits=not args.stock_limits)


def banner(title: str, images: Iterable[Path], **fields: object) -> None:
    """Print the run header: what is being measured, over what, with which knobs."""
    images = list(images)
    print(f"\n{title}")
    print(f"  corpus={len(images)} pages from {images[0].parent}")
    for key, value in fields.items():
        print(f"  {key}={value}")
    preflight_extensions(images)
