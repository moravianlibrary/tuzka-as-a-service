"""Pins how taas-admin prints tables and single objects when piped (no terminal): plain
text, no trailing spaces, empty columns left out, and ``--json`` keeping everything."""

import json
from datetime import datetime

from pydantic import BaseModel

from app.cli.common import output, show


class Row(BaseModel):
    name: str
    enabled: bool
    inflight: int
    note: str | None
    created_at: datetime


class Attempt(BaseModel):
    stage: str
    retry: bool


class Detail(BaseModel):
    name: str
    tags: list[str]
    limits: dict[str, int]
    attempts: list[Attempt]


ROWS = [
    Row(name="gpu1", enabled=True, inflight=12, note=None, created_at=datetime(2026, 10, 26, 9)),
    Row(name="cpu1", enabled=False, inflight=3, note=None, created_at=datetime(2026, 10, 26, 10)),
]


def test_a_table_leaves_out_columns_no_row_has_a_value_in(capsys) -> None:
    output(Row, ROWS, as_json=False)

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["name", "enabled", "inflight", "created", "at"]  # no note
    assert lines[2].split() == ["gpu1", "yes", "12", "2026-10-26", "09:00:00"]
    assert lines[3].split() == ["cpu1", "no", "3", "2026-10-26", "10:00:00"]
    assert lines[2].index("12") + 2 == lines[3].index("3") + 1  # numbers right-aligned
    assert all(line == line.rstrip() for line in lines)


def test_json_keeps_every_field(capsys) -> None:
    output(Row, ROWS, as_json=True)

    (first, _) = json.loads(capsys.readouterr().out)
    assert first["note"] is None and first["created_at"] == "2026-10-26T09:00:00"


def test_an_empty_list_says_so(capsys) -> None:
    output(Row, [], as_json=False)

    assert capsys.readouterr().out.strip() == "(none)"


def test_one_object_prints_field_by_field_with_its_attempts_below(capsys) -> None:
    detail = Detail(
        name="job-1",
        tags=["alto", "txt"],
        limits={"burst": 3},
        attempts=[Attempt(stage="engine", retry=True)],
    )

    show(detail, as_json=False)

    lines = [line.split() for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[:3] == [["name", "job-1"], ["tags", "alto,", "txt"], ["limits", "burst=3"]]
    assert lines[3] == ["attempts"]
    assert lines[4] == ["stage", "retry"] and lines[6] == ["engine", "yes"]
