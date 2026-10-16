"""Tests for output/extension mapping and fmt ↔ outputs conversion."""

from __future__ import annotations

import pytest

from app.engines.outputs import ext_for, fmt_to_outputs, outputs_to_fmt


class TestExtFor:
    def test_known_formats(self) -> None:
        assert ext_for("alto") == "xml"
        assert ext_for("txt") == "txt"

    def test_unknown_uses_name(self) -> None:
        assert ext_for("geojson") == "geojson"
        assert ext_for("json") == "json"


class TestFmtToOutputs:
    def test_multi(self) -> None:
        assert fmt_to_outputs("multi") == frozenset({"alto", "txt"})

    def test_alto(self) -> None:
        assert fmt_to_outputs("alto") == frozenset({"alto"})

    def test_txt(self) -> None:
        assert fmt_to_outputs("txt") == frozenset({"txt"})

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown fmt"):
            fmt_to_outputs("page")


class TestOutputsToFmt:
    def test_alto_txt_to_multi(self) -> None:
        assert outputs_to_fmt(frozenset({"alto", "txt"})) == "multi"

    def test_alto_to_alto(self) -> None:
        assert outputs_to_fmt(frozenset({"alto"})) == "alto"

    def test_txt_to_txt(self) -> None:
        assert outputs_to_fmt(frozenset({"txt"})) == "txt"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown outputs"):
            outputs_to_fmt(frozenset({"page"}))

    def test_round_trip_multi(self) -> None:
        assert outputs_to_fmt(fmt_to_outputs("multi")) == "multi"

    def test_round_trip_alto(self) -> None:
        assert outputs_to_fmt(fmt_to_outputs("alto")) == "alto"

    def test_round_trip_txt(self) -> None:
        assert outputs_to_fmt(fmt_to_outputs("txt")) == "txt"
