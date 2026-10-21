#!/usr/bin/env python
"""Sweep the legacy-vs-new API benchmarks over a scenario file.

One entry point for the whole comparison (`make bench`). Reads `scenarios.toml`,
expands each `[[matrix]]` block into the cartesian product of its list-valued
parameters, runs every case, and writes a results table.

Two methodology details worth knowing, both handled here rather than left to the
person reading the numbers:

* **The latency proxy is all-or-nothing.** If *any* case asks for added RTT, every
  case is routed through the proxy — the zero-latency ones with the delay set to 0.
  Sending some cases direct and others through a proxy would fold the proxy's own
  overhead into the comparison.
* **Every case gets a fresh user** (that's the sub-scripts' default), so no case
  inherits another's rate-limit budget or job history.

Usage:
    python bench/api/run_scenarios.py [--target compose|k3d] [--only NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from common import taas_admin

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

SCRIPTS = {"legacy": "legacy_proarc.py", "new": "new_client.py", "batch": "new_client.py"}
# Fixed arguments a mode adds to its script (batch is new_client.py in batch pacing).
MODE_ARGS = {"batch": ["--batch"]}

# param name in scenarios.toml -> CLI option on the sub-script
COMMON_OPTS = {
    "n": "--n",
    "images": "--images",
    "taas_url": "--taas-url",
    "api_key": "--api-key",
    "timeout": "--timeout",
    "out_dir": "--out-dir",
    "seed": "--seed",
}
COMMON_FLAGS = {"stock_limits": "--stock-limits"}

MODE_OPTS = {
    "legacy": {
        "compat_url": "--compat-url",
        "concurrency": "--concurrency",
        "poll_seconds": "--poll-seconds",
        "engine": "--engine",
    },
    "new": {
        "fmt": "--fmt",
        "domain": "--domain",
        "submit_concurrency": "--submit-concurrency",
    },
    "batch": {"fmt": "--fmt", "domain": "--domain"},
}
# `serial` is a no-op for legacy — ProArc is serial by construction, so
# concurrency=1 already is it. Setting it in a matrix therefore paces both sides
# the same way, which is the point of the serial comparison.
MODE_FLAGS = {
    "legacy": {"reuse_connections": "--reuse-connections"},
    "new": {"serial": "--serial"},
    "batch": {},
}

# Consumed by this runner, never forwarded to a sub-script.
CONTROL_KEYS = {"name", "enabled", "mode", "latency_ms", "jitter_ms", "only_device"}

TABLE_COLUMNS = (
    ("case", "case"),
    ("mode", "mode"),
    ("pages", "pages"),
    ("ok", "ok"),
    ("failed", "fail"),
    ("wall_seconds", "wall s"),
    ("pages_per_minute", "pages/min"),
    ("latency_mean", "lat mean"),
    ("latency_p95", "lat p95"),
    # The decomposition: what the engine did, versus what the protocol cost.
    ("engine_mean", "engine"),
    ("queue_mean", "queue"),
    ("overhead_mean", "overhead"),
    ("http_requests", "requests"),
    ("polls", "polls"),
)


@dataclass
class Case:
    """One concrete run: a matrix name, a short label, and the resolved params."""

    matrix: str
    label: str
    params: dict[str, Any]

    @property
    def mode(self) -> str:
        return str(self.params["mode"])

    @property
    def latency_ms(self) -> int:
        return int(self.params.get("latency_ms", 0))

    @property
    def slug(self) -> str:
        safe = self.label.replace("=", "").replace(",", "-").replace(" ", "")
        return f"{self.matrix}__{safe}" if safe else self.matrix


def load_scenarios(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"scenario file not found: {path}")

    return tomllib.loads(path.read_text(encoding="utf-8"))


def expand(block: dict[str, Any], defaults: dict[str, Any]) -> list[Case]:
    """Expand one [[matrix]] block into its cartesian product.

    List values are swept; scalars are fixed. The label names only the swept keys,
    so `protocol` yields `mode=legacy` rather than repeating every default.
    """
    name = str(block.get("name", "unnamed"))
    swept = {k: v for k, v in block.items() if isinstance(v, list) and k != "name"}
    fixed = {k: v for k, v in block.items() if not isinstance(v, list) and k != "name"}

    if not swept:
        params = {**defaults, **fixed}
        return [Case(matrix=name, label=str(params.get("mode", "")), params=params)]

    cases: list[Case] = []
    keys = sorted(swept)

    for combo in itertools.product(*(swept[k] for k in keys)):
        chosen = dict(zip(keys, combo, strict=True))
        label = ",".join(f"{k}={chosen[k]}" for k in keys)
        cases.append(Case(matrix=name, label=label, params={**defaults, **fixed, **chosen}))

    return cases


def build_plan(config: dict[str, Any], only: set[str], overrides: dict[str, Any]) -> list[Case]:
    """Turn the scenario file into an ordered list of cases to run."""
    defaults = dict(config.get("defaults", {}))
    plan: list[Case] = []

    for block in config.get("matrix", []):
        name = str(block.get("name", "unnamed"))

        # `enabled = false` blocks are opt-in; naming one in --only opts in.
        if only and name not in only:
            continue
        if not only and not block.get("enabled", True):
            continue

        plan.extend(expand(block, defaults))

    for case in plan:
        case.params.update(overrides)
        if case.mode not in SCRIPTS:
            raise SystemExit(f"unknown mode '{case.mode}' in matrix '{case.matrix}'")

        # Sub-scripts run with cwd=bench/api, so paths written the natural way
        # ("test-data") have to be anchored to the repo root, not to this package.
        for key in ("images", "out_dir"):
            value = case.params.get(key)
            if value and not Path(value).is_absolute():
                case.params[key] = str(REPO_ROOT / value)

    return plan


def resolve_target(config: dict[str, Any], target: str, *, proxied: bool) -> dict[str, Any]:
    """Pick the URL set for the chosen target, proxied or direct."""
    targets = config.get("targets", {})
    if target not in targets:
        raise SystemExit(f"unknown target '{target}' (have: {', '.join(sorted(targets))})")

    cfg = dict(targets[target])
    latency_cfg = dict(cfg.pop("latency", {}))
    urls = {k: v for k, v in cfg.items() if k.endswith("_url")}

    if proxied:
        urls |= {k: v for k, v in latency_cfg.items() if k.endswith("_url")}

    return {"urls": urls, "latency": latency_cfg}


def build_argv(case: Case, urls: dict[str, Any], out_json: Path) -> list[str]:
    """Map the case's params onto the right sub-script's CLI."""
    mode = case.mode
    argv = [sys.executable, str(HERE / SCRIPTS[mode]), "--json", str(out_json)]
    argv += MODE_ARGS.get(mode, [])

    opts = {**COMMON_OPTS, **MODE_OPTS[mode]}
    flags = {**COMMON_FLAGS, **MODE_FLAGS[mode]}
    params = {**case.params, **urls}

    for key, value in params.items():
        if key in CONTROL_KEYS or value is None:
            continue

        if key in flags:
            if value:
                argv.append(flags[key])
            continue

        if key in opts:
            argv += [opts[key], str(value)]
            continue

        # A param the *other* mode owns (e.g. --fmt on a legacy case) is skipped
        # silently so one matrix can set both; a genuine typo is not.
        known_elsewhere = any(key in table for table in MODE_OPTS.values()) or any(
            key in table for table in MODE_FLAGS.values()
        )
        if not known_elsewhere:
            raise SystemExit(f"unknown parameter '{key}' in matrix '{case.matrix}'")

    return argv


def proxy(action: str, latency_cfg: dict[str, Any], *, latency_ms: int, jitter_ms: int) -> None:
    """Drive latency-proxy.sh with the target's port wiring."""
    listen = latency_cfg.get("listen", {})
    upstream = latency_cfg.get("upstream", {})

    env_pairs = {
        "LATENCY_MS": str(latency_ms),
        "JITTER_MS": str(jitter_ms),
        "LISTEN_API": str(listen.get("api", 8080)),
        "LISTEN_COMPAT": str(listen.get("compat", 8081)),
        "LISTEN_RESULTS": str(listen.get("results", 9100)),
        "UP_API": str(upstream.get("api", "127.0.0.1:18080")),
        "UP_COMPAT": str(upstream.get("compat", "127.0.0.1:18081")),
        "UP_RESULTS": str(upstream.get("results", "127.0.0.1:19100")),
    }

    result = subprocess.run(
        ["bash", str(HERE / "latency-proxy.sh"), action],
        env={**os.environ, **env_pairs},
    )
    if result.returncode != 0:
        raise SystemExit(f"latency-proxy.sh {action} failed")


def _answers(url: str) -> bool:
    try:
        httpx.get(f"{url.rstrip('/')}/healthz", timeout=5.0)
    except httpx.HTTPError:
        return False

    return True


def ensure_forwards(target_cfg: dict[str, Any], target: str, *, proxied: bool) -> None:
    """Point the k3d port-forwards at whichever layout this run needs.

    The runner already knows whether it needs the proxy, so making the operator
    also know — and remember which of two `make` targets to run first — is pure
    redundancy, and getting it wrong just kills the run. A latency run needs the
    real services on the *upstream* ports so the proxy can own the published ones;
    a plain run needs them on the published ports directly. Either way the wanted
    end state is "the API answers where this run expects it", so check that first
    and only re-point the forwards when it isn't already true.

    k3d only: on compose the services are published by compose itself, and moving
    them means editing `.env.app` (the presigned-URL host) — deliberately left to
    a human, since it mutates a file the operator owns.
    """
    if target != "k3d":
        return

    latency_cfg = target_cfg["latency"]
    wanted = (
        f"http://{latency_cfg['upstream']['api']}"
        if proxied
        else str(target_cfg["urls"]["taas_url"])
    )

    if _answers(wanted):
        return

    forward = REPO_ROOT / "deploy" / "local" / "forward.sh"
    listen = latency_cfg["listen"]
    upstream = latency_cfg["upstream"]

    # Under the proxy the real services move aside to the upstream ports so the
    # proxy can own the published ones; without it they sit on the published ports.
    def port_for(channel: str) -> str:
        if not proxied:
            return str(listen[channel])

        _host, _, port = str(upstream[channel]).rpartition(":")
        return port

    ports = {
        "API_PORT": port_for("api"),
        "COMPAT_PORT": port_for("compat"),
        "RESULTS_PORT": port_for("results"),
    }

    print(f"\nRe-pointing port-forwards for a {'proxied' if proxied else 'direct'} run:")
    for key, value in ports.items():
        print(f"  {key}={value}")

    subprocess.run(["bash", str(forward), "down"], check=False)
    # NO_OPEN: forward.sh xdg-opens the dashboard, which is right when a human runs it
    # and just spawns browser tabs mid-benchmark when the runner does.
    result = subprocess.run(
        ["bash", str(forward), "up"],
        env={**os.environ, **ports, "NO_OPEN": "1"},
    )
    if result.returncode != 0:
        raise SystemExit("could not start port-forwards — is the k3d stack up?")

    if not _answers(wanted):
        raise SystemExit(f"port-forwards started but nothing answers on {wanted}")


def check_upstream(latency_cfg: dict[str, Any]) -> None:
    """Fail early if the real API isn't where the proxy expects it."""
    upstream = latency_cfg.get("upstream", {}).get("api")
    if not upstream:
        return

    if not _answers(f"http://{upstream}"):
        requires = latency_cfg.get("requires", "").strip()
        raise SystemExit(
            f"nothing answering on {upstream}, which the latency proxy needs as its "
            f"upstream.\n\n{requires}\n"
        )


def pin_backends(device: str) -> list[str]:
    """Enable only the backends of one device class, disabling the rest.

    A run that spreads over a ~1 s GPU engine and a ~5 s CPU engine has bimodal
    engine time, which shows up as noise in every derived figure. Pinning to one
    device class makes engine time a stable baseline so the protocol difference is
    what moves. Declared in scenarios.toml (`only_device`) rather than done by hand,
    so a run is reproducible.

    Note this makes backend *priority* irrelevant: with a single enabled backend
    there is nothing to order or spill to.

    Returns a human-readable description of the resulting state.
    """
    backends = json.loads(taas_admin("backend", "list", "--json"))
    wanted = [b for b in backends if b.get("device") == device]
    if not wanted:
        devices = sorted({str(b.get("device")) for b in backends})
        raise SystemExit(f"only_device={device!r} matches no backend (have: {', '.join(devices)})")

    described: list[str] = []
    for backend in backends:
        should_be = backend.get("device") == device
        if backend.get("enabled") != should_be:
            taas_admin("backend", "enable" if should_be else "disable", str(backend["id"]))
        state = "enabled" if should_be else "disabled"
        described.append(f"{backend['label']} ({backend.get('device')}) {state}")
    return described


def _row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    # Target is part of the identity: a compose row and a k3d row for the same case
    # are different measurements and must not overwrite each other.
    return (str(row.get("target")), str(row.get("matrix")), str(row.get("case")))


def merge_rows(out_dir: Path, fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold this run's rows into whatever previous runs recorded.

    Without this, `ONLY=one-matrix` would rewrite RESULTS_API.md with just that
    matrix and silently drop the rest. Re-running a case overwrites its own row.
    """
    ledger = out_dir / "_rows.json"
    previous: list[dict[str, Any]] = []

    if ledger.is_file():
        try:
            previous = json.loads(ledger.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = []

    merged: dict[tuple[str, str, str], dict[str, Any]] = {_row_key(row): row for row in previous}
    for row in fresh:
        merged[_row_key(row)] = row

    rows = list(merged.values())
    ledger.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return rows


def render_table(rows: list[dict[str, Any]]) -> str:
    """Markdown table, grouped by matrix."""
    lines: list[str] = []

    for target, matrix in dict.fromkeys((r.get("target"), r["matrix"]) for r in rows):
        group = [r for r in rows if r.get("target") == target and r["matrix"] == matrix]
        lines += [f"### {matrix} — target `{target}`", ""]
        lines.append("| " + " | ".join(label for _, label in TABLE_COLUMNS) + " |")
        lines.append("|" + "---|" * len(TABLE_COLUMNS))

        for row in group:
            cells = [str(row.get(key, "")) for key, _ in TABLE_COLUMNS]
            lines.append("| " + " | ".join(cells) + " |")

        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=HERE / "scenarios.toml")
    parser.add_argument("--target", default="compose", help="target profile in the scenario file")
    parser.add_argument("--only", default="", help="comma-separated matrix names to run")
    parser.add_argument("--out", type=Path, default=HERE / "out")
    parser.add_argument("--results", type=Path, default=HERE / "RESULTS_API.md")
    parser.add_argument("--n", type=int, default=None, help="override pages per case")
    parser.add_argument("--images", type=Path, default=None, help="override the corpus dir")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="corpus sample seed; omit to draw a fresh one (printed, so runs reproduce)",
    )
    args = parser.parse_args()
    # taas-admin (user provisioning, backend pinning) runs in this target's api container;
    # the sub-scripts inherit it.
    os.environ.setdefault("TAAS_ADMIN_MODE", args.target)

    config = load_scenarios(args.scenarios)
    only = {name.strip() for name in args.only.split(",") if name.strip()}

    overrides: dict[str, Any] = {}
    if args.n is not None:
        overrides["n"] = args.n

    # One seed for the whole sweep, never one per case: every case must bench the
    # SAME pages or corpus variance leaks into the comparison between them. Drawn
    # fresh each run so results aren't tied to one fixed slice of the corpus, and
    # printed so any run can be reproduced exactly with --seed.
    overrides["seed"] = args.seed if args.seed is not None else random.randrange(1_000_000)
    if args.images is not None:
        overrides["images"] = str(args.images)

    plan = build_plan(config, only, overrides)
    if not plan:
        raise SystemExit(f"nothing to run (--only {args.only!r} matched no enabled matrix)")

    needs_proxy = any(case.latency_ms > 0 for case in plan)
    target = resolve_target(config, args.target, proxied=needs_proxy)

    print(
        f"\ntarget={args.target}  cases={len(plan)}  "
        f"proxy={'on' if needs_proxy else 'off'}  seed={overrides['seed']}"
    )
    for key, value in target["urls"].items():
        print(f"  {key}={value}")
    print()
    for index, case in enumerate(plan, 1):
        pages = case.params.get("n")
        print(f"  {index:>3}. {case.matrix:<24} {case.label:<28} pages={pages}")

    # Serial cases cost pages x seconds-per-page; all-in-flight ones overlap and
    # finish in roughly one page-time plus the queue drain. Legacy is always serial.
    serial_pages = sum(
        int(c.params.get("n", 0)) for c in plan if c.mode == "legacy" or c.params.get("serial")
    )
    total_pages = sum(int(c.params.get("n", 0)) for c in plan)
    print(
        f"\n  {total_pages} page-runs total, {serial_pages} of them one-page-at-a-time.\n"
        f"  At ~10 s/page those alone are roughly {serial_pages * 10 / 60:.0f} min "
        f"— trim with N= or ONLY=."
    )

    if args.dry_run:
        print("\ndry run — nothing executed")
        return

    ensure_forwards(target, args.target, proxied=needs_proxy)

    if needs_proxy:
        check_upstream(target["latency"])

    # Backend pinning is setup, not measurement, so it bypasses the latency proxy.
    # It cannot just use the unproxied URL though: under the k3d wiring the proxy
    # *takes over* the published port and the real service moves to the upstream one,
    # so the un-delayed address is the upstream, not the nominal one. (And the proxy
    # isn't running yet at this point either way.)
    # Per-target (the deployment decides what engines exist), defaults as fallback.
    target_cfg = config.get("targets", {}).get(args.target, {})
    only_device = target_cfg.get("only_device") or config.get("defaults", {}).get("only_device")
    if only_device:
        print(f"\nPinning dispatch to device={only_device} with taas-admin")
        print("  (priority is moot with a single enabled backend)")
        for line in pin_backends(str(only_device)):
            print(f"  {line}")

    args.out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    current_latency: int | None = None

    try:
        for index, case in enumerate(plan, 1):
            if needs_proxy and case.latency_ms != current_latency:
                action = "up" if current_latency is None else "set"
                proxy(
                    action,
                    target["latency"],
                    latency_ms=case.latency_ms,
                    jitter_ms=int(case.params.get("jitter_ms", 0)),
                )
                current_latency = case.latency_ms

            out_json = args.out / f"{case.slug}.json"
            argv = build_argv(case, target["urls"], out_json)

            elapsed = time.monotonic() - started
            print(f"\n{'=' * 78}")
            print(
                f"[{index}/{len(plan)}] {case.matrix}  {case.label}  "
                f"(rtt={case.latency_ms}ms, elapsed {elapsed / 60:.1f}m)"
            )
            print(f"{'=' * 78}")

            result = subprocess.run(argv, cwd=HERE)
            if result.returncode != 0 or not out_json.is_file():
                print(f"  !! case failed (exit {result.returncode}) — recorded as such")
                rows.append(
                    {
                        "target": args.target,
                        "matrix": case.matrix,
                        "case": case.label,
                        "mode": case.mode,
                        "failed": "ERR",
                    }
                )
                continue

            summary = json.loads(out_json.read_text(encoding="utf-8"))
            rows.append(
                {
                    "target": args.target,
                    "matrix": case.matrix,
                    "case": case.label,
                    **{k: v for k, v in summary.items() if k not in {"timings", "notes", "errors"}},
                    "latency_ms": case.latency_ms,
                }
            )
    finally:
        if needs_proxy:
            proxy("down", target["latency"], latency_ms=0, jitter_ms=0)

    table = render_table(merge_rows(args.out, rows))
    total = (time.monotonic() - started) / 60

    header = (
        f"# API benchmark — legacy (ProArc/PERO) vs new (taas client)\n\n"
        f"Generated by `make bench` (`bench/api/run_scenarios.py`), target `{args.target}`. "
        f"Last run: {len(plan)} case(s) in {total:.1f} min; earlier matrices are carried "
        f"over from `out/_rows.json` (delete it to start clean).\n\n"
        f"In `protocol` read **pages/min** — the new run has everything in flight, so its "
        f"per-page latency absorbs queue time. In `serial-per-page` both sides run one page "
        f"at a time, so **lat mean** is the comparable number there. See "
        f"[README.md](README.md).\n\n"
    )
    args.results.write_text(header + table + "\n", encoding="utf-8")

    print(f"\n{table}")
    print(f"\ntotal {total:.1f} min — wrote {args.results} and per-case JSON to {args.out}/")


if __name__ == "__main__":
    main()
