#!/usr/bin/env python3
"""Up-hours scheduler for a taas OCR box.

Keeps the box's engine running only inside a configured weekly schedule, so a machine
that has another day job (a work PC, a shared workstation) can serve OCR off-hours and
be left alone the rest of the time.

Every tick it reconciles *observed* state against the schedule, so it self-heals after a
manual `docker compose up`, a reboot, or a suspend/resume — there is no persistent state
to get out of sync.

Shutdown is two-phase, and the tunnel stays up throughout:

  1. At window end the dispatch gate closes (a flag file the nginx `gate` service
     checks). taas health-checks the engine before dispatching, so within
     ``health_cache_seconds`` no new pages are sent — while /api/v1/status and
     /api/v1/result keep passing through, so the poller can still harvest the pages the
     engine is finishing.
  2. After OCR_DRAIN_MINUTES the engine is stopped, releasing the GPU.

Only pages that are still unfinished when the drain expires are lost, and those are
requeued by the cluster poller (an unreachable engine is already a retryable state).
The tunnel is left running by default so the box stays observable (cAdvisor / GPU
metrics keep flowing); set OCR_STOP_TUNNEL=1 to stop it after the engine.

Controlled containers are found by their compose labels, so both the `registry` and
`build` engine profiles work with no configuration: a service that doesn't exist matches
nothing and is skipped.

Config (env):
  OCR_UP_SCHEDULE     weekly windows, e.g. "Mon-Fri 17:30-07:30; Sat,Sun 00:00-24:00"
                      "always" (default) or "never" disable/invert the whole thing
  OCR_DRAIN_MINUTES   grace between closing the gate and stopping the engine (10)
  OCR_POLL_SECONDS    reconcile interval (60)
  OCR_STOP_TIMEOUT    `docker stop -t` grace, seconds (120)
  OCR_STOP_TUNNEL     1 = also stop frpc after the engine (default 0, tunnel stays up)
  OCR_GATE_DIR        shared volume holding the gate flag (/gate)
  TZ                  timezone the schedule is written in (e.g. Europe/Prague)
  SCHED_ENGINE_SERVICES / SCHED_TUNNEL_SERVICES / SCHED_GATE_SERVICES
                      compose services to control
  SCHED_PROJECT       compose project name (auto-detected from our own container)
"""

from __future__ import annotations

import os
import platform
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_DAY_NUMBERS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
_ALL_DAYS = frozenset(range(7))
_MINUTES_PER_DAY = 24 * 60

# Whole-week aliases accepted in place of a day/time spec.
_ALWAYS_WORDS = {"", "always", "all", "24/7"}
_NEVER_WORDS = {"never", "off", "none"}

# Entries are separated by ";" or by a comma that follows a time range (a comma after a
# digit). That keeps day lists unambiguous: "Sat,Sun 00:00-24:00" is one entry, while
# "Mon-Fri 17:30-07:30, Sat,Sun 00:00-24:00" is two.
_ENTRY_SPLIT = re.compile(r";|(?<=\d)\s*,")
_ENTRY_RE = re.compile(
    r"^(?P<days>[A-Za-z,\- ]+?)\s+(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})$"
)


class ScheduleError(ValueError):
    """The OCR_UP_SCHEDULE expression could not be parsed."""


@dataclass(frozen=True)
class Window:
    """An up-window: on each day in ``days``, from ``start`` to ``end`` minutes past
    midnight. ``end < start`` means the window wraps past midnight into the next day
    (so "Fri 22:00-06:00" ends Saturday morning, whether or not Sat is listed)."""

    days: frozenset[int]
    start: int
    end: int


def parse_time(token: str) -> int:
    """Minutes past midnight for "HH:MM". Accepts "24:00" as end-of-day."""
    hours, _, minutes = token.partition(":")
    try:
        total = int(hours) * 60 + int(minutes)
    except ValueError as exc:
        raise ScheduleError(f"bad time {token!r} (want HH:MM)") from exc
    if not 0 <= total <= _MINUTES_PER_DAY or int(minutes) > 59:
        raise ScheduleError(f"time out of range: {token!r}")
    return total


def parse_days(token: str) -> frozenset[int]:
    """Day set for "Mon", "Mon-Fri", "Sat,Sun" or "Fri-Mon" (ranges may wrap)."""
    days: set[int] = set()
    for part in token.replace(" ", "").split(","):
        if not part:
            continue
        if part.lower() in {"daily", "everyday", "all"}:
            days |= _ALL_DAYS
            continue
        first, sep, last = part.partition("-")
        start = _DAY_NUMBERS.get(first.lower()[:3])
        if start is None:
            raise ScheduleError(f"unknown day {first!r}")
        if not sep:
            days.add(start)
            continue
        end = _DAY_NUMBERS.get(last.lower()[:3])
        if end is None:
            raise ScheduleError(f"unknown day {last!r}")
        # Inclusive range, wrapping through Sunday: Fri-Mon == Fri,Sat,Sun,Mon.
        span = (end - start) % 7
        days |= {(start + offset) % 7 for offset in range(span + 1)}
    if not days:
        raise ScheduleError(f"no days in {token!r}")
    return frozenset(days)


def parse_schedule(text: str) -> tuple[Window, ...]:
    """Parse an OCR_UP_SCHEDULE expression into windows.

    "always" (and the empty string) yield one all-week window; "never" yields no
    windows, which `is_up` reports as permanently down."""
    cleaned = text.strip()
    if cleaned.lower() in _ALWAYS_WORDS:
        return (Window(_ALL_DAYS, 0, _MINUTES_PER_DAY),)
    if cleaned.lower() in _NEVER_WORDS:
        return ()

    windows: list[Window] = []
    for chunk in _ENTRY_SPLIT.split(cleaned):
        entry = chunk.strip()
        if not entry:
            continue
        match = _ENTRY_RE.match(entry)
        if not match:
            raise ScheduleError(f"bad entry {entry!r} (want 'Mon-Fri 18:00-07:00')")
        start = parse_time(match["start"])
        end = parse_time(match["end"])
        if start == end:
            raise ScheduleError(f"empty window in {entry!r} (use 00:00-24:00 for all day)")
        windows.append(Window(parse_days(match["days"]), start, end))
    if not windows:
        raise ScheduleError(f"no windows in {text!r}")
    return tuple(windows)


def is_up(windows: tuple[Window, ...], now: datetime) -> bool:
    """True if ``now`` falls inside any window."""
    minute = now.hour * 60 + now.minute
    today = now.weekday()
    yesterday = (today - 1) % 7
    for window in windows:
        if window.end > window.start:
            if today in window.days and window.start <= minute < window.end:
                return True
            continue
        # Wrapping window: the tail belongs to the day *after* each listed day.
        if today in window.days and minute >= window.start:
            return True
        if yesterday in window.days and minute < window.end:
            return True
    return False


@dataclass(frozen=True)
class Plan:
    """What to do this tick. ``action`` is one of:

    start        — open the gate and bring the containers up
    close        — close the gate and start the drain clock (engine keeps working)
    stop_engine  — drain is over, stop the engine (and the tunnel if configured)
    close_stop   — close the gate and stop the engine at once (drain disabled)
    wait         — draining, deadline not reached
    none         — observed state already matches the schedule
    """

    action: str
    drain_deadline: datetime | None = None


def plan(
    *,
    want_up: bool,
    gate_closed: bool,
    engine_running: bool,
    drain_deadline: datetime | None,
    now: datetime,
    drain: timedelta,
) -> Plan:
    """Decide this tick's action from observed state. Pure, so it's unit-testable."""
    if want_up:
        if engine_running and not gate_closed:
            return Plan("none")
        return Plan("start")

    if not engine_running:
        # Nothing left to drain; just make sure the gate reflects the schedule.
        return Plan("none") if gate_closed else Plan("close_stop")

    if drain <= timedelta(0):
        return Plan("close_stop")

    # Either the gate is still open (the drain starts now), or we found it closed with
    # no deadline — a scheduler restart mid-drain, so give the engine a fresh grace
    # period rather than killing it outright.
    if not gate_closed or drain_deadline is None:
        return Plan("close", now + drain)

    if now >= drain_deadline:
        return Plan("stop_engine")
    return Plan("wait", drain_deadline)


# ------------------------------------------------------------------------------- gate


class Gate:
    """The dispatch gate's flag file, shared with the nginx `gate` service."""

    def __init__(self, directory: str) -> None:
        self.flag = Path(directory) / "closed"

    def is_closed(self) -> bool:
        return self.flag.exists()

    def close(self) -> None:
        self.flag.parent.mkdir(parents=True, exist_ok=True)
        self.flag.write_text("closed by the up-hours scheduler\n")

    def open(self) -> None:
        self.flag.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- docker


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _service_containers(project: str, services: list[str], *, running_only: bool) -> list[str]:
    ids: list[str] = []
    for service in services:
        args = ["ps", "-q"] if running_only else ["ps", "-aq"]
        args += [
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ]
        ids += [line for line in _docker(*args).splitlines() if line]
    return ids


def detect_project() -> str:
    """Compose project of the containers we manage — explicit config, else our own."""
    configured = os.environ.get("SCHED_PROJECT") or os.environ.get("COMPOSE_PROJECT_NAME")
    if configured:
        return configured
    own = os.environ.get("HOSTNAME") or platform.node()
    label = _docker(
        "inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}', own
    )
    if not label or label == "<no value>":
        raise RuntimeError(
            "could not detect the compose project name — set SCHED_PROJECT in the environment"
        )
    return label


# ----------------------------------------------------------------------------- runner


@dataclass(frozen=True)
class Config:
    windows: tuple[Window, ...]
    schedule_text: str
    drain: timedelta
    poll: float
    stop_timeout: int
    stop_tunnel: bool
    gate_dir: str
    engine_services: list[str]
    tunnel_services: list[str]
    gate_services: list[str]


def _csv(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    text = os.environ.get("OCR_UP_SCHEDULE", "always")
    return Config(
        windows=parse_schedule(text),
        schedule_text=text.strip() or "always",
        drain=timedelta(minutes=float(os.environ.get("OCR_DRAIN_MINUTES", "10"))),
        poll=float(os.environ.get("OCR_POLL_SECONDS", "60")),
        stop_timeout=int(os.environ.get("OCR_STOP_TIMEOUT", "120")),
        stop_tunnel=_flag("OCR_STOP_TUNNEL"),
        gate_dir=os.environ.get("OCR_GATE_DIR", "/gate"),
        engine_services=_csv("SCHED_ENGINE_SERVICES", "tuzkaocr,tuzkaocr-build"),
        tunnel_services=_csv("SCHED_TUNNEL_SERVICES", "frpc"),
        gate_services=_csv("SCHED_GATE_SERVICES", "gate"),
    )


def log(message: str) -> None:
    print(f"[schedule] {datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def run(cfg: Config, project: str, gate: Gate, stop: threading.Event) -> None:
    startable = cfg.engine_services + cfg.tunnel_services + cfg.gate_services
    deadline: datetime | None = None
    last_logged: str | None = None

    while not stop.is_set():
        now = datetime.now()
        decision = Plan("none")
        try:
            engine_up = _service_containers(project, cfg.engine_services, running_only=True)
            decision = plan(
                want_up=is_up(cfg.windows, now),
                gate_closed=gate.is_closed(),
                engine_running=bool(engine_up),
                drain_deadline=deadline,
                now=now,
                drain=cfg.drain,
            )

            if decision.action == "start":
                ids = _service_containers(project, startable, running_only=False)
                if not ids:
                    # Nothing to start (never brought up, or `compose down`): idle
                    # quietly instead of retrying every few seconds.
                    decision = Plan("none")
                    if last_logged != "absent":
                        log("in window, but no box containers exist — run `docker compose up -d`")
                        last_logged = "absent"
                else:
                    gate.open()
                    _docker("start", *ids)
                    deadline = None
                    log(f"in window — gate open, {len(ids)} container(s) started")
            elif decision.action == "close":
                gate.close()
                deadline = decision.drain_deadline
                log(
                    f"out of window — gate closed, draining engine until {deadline:%H:%M:%S} "
                    f"({cfg.drain.total_seconds() / 60:g} min)"
                )
            elif decision.action in {"stop_engine", "close_stop"}:
                gate.close()
                to_stop = list(engine_up)
                if cfg.stop_tunnel:
                    to_stop += _service_containers(project, cfg.tunnel_services, running_only=True)
                if to_stop:
                    _docker("stop", "-t", str(cfg.stop_timeout), *to_stop)
                deadline = None
                reason = "drain over" if decision.action == "stop_engine" else "out of window"
                log(f"{reason} — stopped {len(to_stop)} container(s)")
            else:
                state = "draining" if decision.action == "wait" else ("up" if engine_up else "down")
                if state != last_logged:
                    log(f"steady state: {state}")
                    last_logged = state
        except subprocess.CalledProcessError as exc:
            log(f"docker command failed ({exc.returncode}): {(exc.stderr or '').strip()}")
        except Exception as exc:  # keep the loop alive; the next tick reconciles again
            log(f"tick failed: {exc}")

        # After acting, re-check soon so the next step of a transition (close -> stop)
        # isn't delayed by a whole poll interval; otherwise idle at the poll interval.
        if decision.action in {"none", "wait"}:
            stop.wait(cfg.poll)
        else:
            last_logged = None
            stop.wait(min(cfg.poll, 5.0))


def main() -> int:
    try:
        cfg = load_config()
    except (ScheduleError, ValueError) as exc:
        print(f"[schedule] invalid configuration: {exc}", file=sys.stderr, flush=True)
        return 2

    project = detect_project()
    tz = datetime.now().astimezone().tzname()
    log(f"project={project} schedule={cfg.schedule_text!r} tz={tz}")
    log(
        f"engine={','.join(cfg.engine_services)} gate={','.join(cfg.gate_services)} "
        f"drain={cfg.drain.total_seconds() / 60:g}min poll={cfg.poll:g}s "
        f"stop_tunnel={int(cfg.stop_tunnel)}"
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    run(cfg, project, Gate(cfg.gate_dir), stop)
    log("exiting — gate and containers left as-is")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
