"""Tests for output/extension mapping and the legacy fmt ↔ outputs conversion."""

from __future__ import annotations

import pytest

from app.engines.outputs import ext_for
from app.vocabulary import Fmt


class TestExtFor:
    def test_known_formats(self) -> None:
        assert ext_for("alto") == "xml"
        assert ext_for("txt") == "txt"

    def test_unknown_uses_name(self) -> None:
        assert ext_for("geojson") == "geojson"
        assert ext_for("json") == "json"


class TestFmtOutputs:
    def test_multi(self) -> None:
        assert Fmt.MULTI.outputs == frozenset({"alto", "txt"})

    def test_alto(self) -> None:
        assert Fmt.ALTO.outputs == frozenset({"alto"})

    def test_txt(self) -> None:
        assert Fmt.TXT.outputs == frozenset({"txt"})

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            Fmt("page")


class TestFmtFromOutputs:
    def test_alto_txt_to_multi(self) -> None:
        assert Fmt.from_outputs(frozenset({"alto", "txt"})) is Fmt.MULTI

    def test_alto_to_alto(self) -> None:
        assert Fmt.from_outputs(frozenset({"alto"})) is Fmt.ALTO

    def test_txt_to_txt(self) -> None:
        assert Fmt.from_outputs(frozenset({"txt"})) is Fmt.TXT

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown outputs"):
            Fmt.from_outputs(frozenset({"page"}))

    @pytest.mark.parametrize("fmt", list(Fmt))
    def test_round_trip(self, fmt: Fmt) -> None:
        assert Fmt.from_outputs(fmt.outputs) is fmt
