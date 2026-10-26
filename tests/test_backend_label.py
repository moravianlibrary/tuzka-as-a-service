"""Pins Backend.display_label: the label, or the id when there is none, in Python and SQL."""

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.backend import Backend


def test_display_label_falls_back_to_the_id() -> None:
    assert Backend(id=3, url="http://engine", label="box1-gpu1").display_label == "box1-gpu1"
    assert Backend(id=3, url="http://engine").display_label == "3"


def test_display_label_selects_the_same_fallback() -> None:
    sql = str(select(Backend.display_label).compile(dialect=postgresql.dialect()))
    assert "coalesce(backends.label, CAST(backends.id AS VARCHAR))" in sql
