"""Pins one tick of the box scheduler's loop: what it does to the gate and the
containers for each plan, against a fake ``docker``."""

import subprocess
import threading
from datetime import timedelta

import pytest

from tests.test_box_schedule import schedule

PROJECT = "box"


class OneTick(threading.Event):
    """Stops the loop at its first wait, so ``run`` makes exactly one tick."""

    def wait(self, timeout: float | None = None) -> bool:
        self.set()
        return True


class FakeDocker:
    """Containers by compose service, each running or not; records start and stop."""

    def __init__(self, containers: dict[str, dict[str, bool]]) -> None:
        self.containers = containers  # service → container id → running
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str:
        if args[0] != "ps":
            self.calls.append(args)
            return ""
        running_only = args[1] == "-q"
        service = args[-1].removeprefix("label=com.docker.compose.service=")
        ids = self.containers.get(service, {})
        return "\n".join(i for i, running in ids.items() if running or not running_only)


def _config(schedule_text: str, **fields) -> object:
    base = {
        "windows": schedule.parse_schedule(schedule_text),
        "schedule_text": schedule_text,
        "drain": timedelta(minutes=10),
        "poll": 60.0,
        "stop_timeout": 120,
        "stop_tunnel": False,
        "gate_dir": "",
        "engine_services": ["tuzkaocr"],
        "tunnel_services": ["frpc"],
        "gate_services": ["gate"],
    }
    return schedule.Config(**(base | fields))


def _closed_gate(tmp_path) -> object:
    gate = schedule.Gate(str(tmp_path))
    gate.close()
    return gate


def _tick(monkeypatch, docker: FakeDocker, cfg, gate) -> None:
    monkeypatch.setattr(schedule, "_docker", docker)
    schedule.run(cfg, PROJECT, gate, OneTick())


def test_in_window_opens_the_gate_and_starts_the_box(monkeypatch, tmp_path, capsys) -> None:
    docker = FakeDocker({"tuzkaocr": {"e1": False}, "frpc": {"t1": True}, "gate": {"g1": True}})

    gate = _closed_gate(tmp_path)
    _tick(monkeypatch, docker, _config("always"), gate)

    assert not gate.is_closed()
    assert docker.calls == [("start", "e1", "t1", "g1")]
    assert "gate open, 3 container(s) started" in capsys.readouterr().out


def test_in_window_without_containers_only_says_so(monkeypatch, tmp_path, capsys) -> None:
    docker = FakeDocker({})

    gate = _closed_gate(tmp_path)
    _tick(monkeypatch, docker, _config("always"), gate)

    assert gate.is_closed()
    assert docker.calls == []
    assert "no box containers exist" in capsys.readouterr().out


def test_window_end_closes_the_gate_and_lets_the_engine_drain(monkeypatch, tmp_path) -> None:
    docker = FakeDocker({"tuzkaocr": {"e1": True}})

    gate = schedule.Gate(str(tmp_path))
    _tick(monkeypatch, docker, _config("never"), gate)

    assert gate.is_closed()
    assert docker.calls == []  # the engine keeps finishing its pages


def test_without_a_drain_the_engine_stops_at_once(monkeypatch, tmp_path) -> None:
    docker = FakeDocker({"tuzkaocr": {"e1": True}, "frpc": {"t1": True}})

    gate = schedule.Gate(str(tmp_path))
    _tick(monkeypatch, docker, _config("never", drain=timedelta(0)), gate)

    assert gate.is_closed()
    assert docker.calls == [("stop", "-t", "120", "e1")]  # the tunnel stays up


def test_the_tunnel_stops_too_when_configured(monkeypatch, tmp_path) -> None:
    docker = FakeDocker({"tuzkaocr": {"e1": True}, "frpc": {"t1": True}})
    cfg = _config("never", drain=timedelta(0), stop_tunnel=True)

    _tick(monkeypatch, docker, cfg, schedule.Gate(str(tmp_path)))

    assert docker.calls == [("stop", "-t", "120", "e1", "t1")]


def test_a_failing_docker_command_is_logged_not_raised(monkeypatch, tmp_path, capsys) -> None:
    def docker(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["docker", *args], stderr="daemon down")

    _tick(monkeypatch, docker, _config("always"), schedule.Gate(str(tmp_path)))  # type: ignore[arg-type]

    assert "docker command failed (1): daemon down" in capsys.readouterr().out


@pytest.mark.parametrize(("engine_running", "state"), [(True, "up"), (False, "down")])
def test_a_steady_state_is_logged(monkeypatch, tmp_path, capsys, engine_running, state) -> None:
    docker = FakeDocker({"tuzkaocr": {"e1": engine_running}})
    # Up in its window with the gate open, or down outside it with the gate closed.
    cfg = _config("always" if engine_running else "never")
    gate = schedule.Gate(str(tmp_path)) if engine_running else _closed_gate(tmp_path)

    _tick(monkeypatch, docker, cfg, gate)

    assert docker.calls == []
    assert f"steady state: {state}" in capsys.readouterr().out
