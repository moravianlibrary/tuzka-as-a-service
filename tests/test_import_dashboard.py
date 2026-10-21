"""Tests for the pure parts of the HyperDX dashboard importer (step 5 T6)."""

import json

import pytest

from app.ops.import_dashboard import load_dashboards, plan_import, resolve_sources

DASH = {
    "version": "0.1.0",
    "name": "taas",
    "tiles": [
        {"id": "a", "config": {"name": "Throughput", "source": "Job Events"}},
        {"id": "b", "config": {"name": "Queue depth", "source": "Live Metrics"}},
        {"id": "c", "config": {"name": "Notes", "displayType": "markdown"}},
    ],
}


def test_load_one_or_many() -> None:
    assert load_dashboards(json.dumps(DASH)) == [DASH]
    assert len(load_dashboards(json.dumps([DASH, {**DASH, "name": "other"}]))) == 2
    with pytest.raises(ValueError):
        load_dashboards(json.dumps({"tiles": []}))


def test_plan_updates_by_name_and_creates_the_rest() -> None:
    other = {**DASH, "name": "new"}
    existing = [{"id": "d1", "name": "taas"}, {"id": "d2", "name": "someone else's"}]
    assert plan_import([DASH, other], existing) == [
        ("update", DASH, "d1"),
        ("create", other, None),
    ]


def test_resolve_sources_maps_names_to_ids() -> None:
    resolved = resolve_sources(DASH, {"Job Events": "s1", "Live Metrics": "s2"})
    assert [t["config"].get("source") for t in resolved["tiles"]] == ["s1", "s2", None]
    assert DASH["tiles"][0]["config"]["source"] == "Job Events"  # input untouched


def test_resolve_sources_names_what_is_missing() -> None:
    with pytest.raises(ValueError, match="Live Metrics"):
        resolve_sources(DASH, {"Job Events": "s1"})
