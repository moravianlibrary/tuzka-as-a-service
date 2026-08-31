"""Pins the box up-hours scheduler: schedule parsing, window membership, and the
drain state machine (deploy/box/scheduler/schedule.py — shipped to boxes, not importable
as part of the app package, so it's loaded by path)."""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy/box/scheduler/schedule.py"
_spec = importlib.util.spec_from_file_location("box_schedule", _MODULE_PATH)
assert _spec and _spec.loader
schedule = importlib.util.module_from_spec(_spec)
# Register before exec: @dataclass resolves its own module out of sys.modules.
sys.modules["box_schedule"] = schedule
_spec.loader.exec_module(schedule)

MON, TUE, FRI, SAT, SUN = 0, 1, 4, 5, 6


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A datetime on a known weekday — 2026-08-31 is a Monday, so day=0 is Monday."""
    return datetime(2026, 8, 31, hour, minute) + timedelta(days=day)


def test_always_and_never():
    assert schedule.is_up(schedule.parse_schedule("always"), at(MON, 12))
    assert schedule.is_up(schedule.parse_schedule(""), at(SAT, 3))
    assert not schedule.is_up(schedule.parse_schedule("never"), at(MON, 12))


def test_wrapping_window_covers_the_night_but_not_the_workday():
    windows = schedule.parse_schedule("Mon-Fri 17:30-07:30")
    assert not schedule.is_up(windows, at(MON, 12))
    assert schedule.is_up(windows, at(MON, 17, 30))
    assert schedule.is_up(windows, at(MON, 23))
    # Tuesday 06:00 belongs to Monday's window; 07:30 is the exclusive end.
    assert schedule.is_up(windows, at(TUE, 6))
    assert not schedule.is_up(windows, at(TUE, 7, 30))
    # Saturday 06:00 is the tail of Friday's window; Saturday evening is not covered.
    assert schedule.is_up(windows, at(SAT, 6))
    assert not schedule.is_up(windows, at(SAT, 20))


def test_comma_separated_entries_and_day_lists():
    # The comma after "07:30" separates entries; the one in "Sat,Sun" does not.
    windows = schedule.parse_schedule("Mon-Fri 17:30-07:30, Sat,Sun 00:00-24:00")
    assert len(windows) == 2
    assert schedule.is_up(windows, at(SAT, 13))
    assert schedule.is_up(windows, at(SUN, 23, 59))
    assert not schedule.is_up(windows, at(MON, 9))


def test_semicolon_separator_and_wrapping_day_range():
    windows = schedule.parse_schedule("Fri-Mon 20:00-06:00 ; Wed 00:00-24:00")
    assert windows[0].days == frozenset({FRI, SAT, SUN, MON})
    assert schedule.is_up(windows, at(SUN, 21))
    assert not schedule.is_up(windows, at(TUE, 21))


@pytest.mark.parametrize(
    "text",
    [
        "Mon 18:00",  # no range
        "Fun 18:00-20:00",  # unknown day
        "Mon 18:00-18:00",  # empty window
        "Mon 25:00-26:00",  # out of range
        "18:00-20:00",  # no days
    ],
)
def test_rejects_bad_expressions(text):
    with pytest.raises(schedule.ScheduleError):
        schedule.parse_schedule(text)


# ------------------------------------------------------------------ drain state machine

DRAIN = timedelta(minutes=10)
NOW = at(MON, 18)


def plan(**kwargs):
    base = dict(
        want_up=False,
        gate_closed=False,
        engine_running=True,
        drain_deadline=None,
        now=NOW,
        drain=DRAIN,
    )
    return schedule.plan(**{**base, **kwargs})


def test_in_window_starts_and_then_idles():
    assert plan(want_up=True, gate_closed=True).action == "start"
    assert plan(want_up=True, engine_running=False).action == "start"
    assert plan(want_up=True).action == "none"


def test_window_end_closes_the_gate_before_stopping_the_engine():
    first = plan()
    assert first.action == "close"
    assert first.drain_deadline == NOW + DRAIN
    # Engine keeps running (and results keep flowing out) until the deadline passes.
    assert plan(gate_closed=True, drain_deadline=first.drain_deadline).action == "wait"
    assert (
        plan(
            gate_closed=True,
            drain_deadline=first.drain_deadline,
            now=NOW + DRAIN,
        ).action
        == "stop_engine"
    )


def test_restart_mid_drain_gives_the_engine_a_fresh_grace_period():
    # Gate already closed but no deadline in memory: don't kill the engine outright.
    resumed = plan(gate_closed=True, drain_deadline=None)
    assert resumed.action == "close"
    assert resumed.drain_deadline == NOW + DRAIN


def test_zero_drain_closes_and_stops_in_one_step():
    assert plan(drain=timedelta(0)).action == "close_stop"


def test_engine_already_down_only_reconciles_the_gate():
    assert plan(engine_running=False).action == "close_stop"
    assert plan(engine_running=False, gate_closed=True).action == "none"
