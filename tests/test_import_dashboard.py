"""Tests for the pure parts of the HyperDX dashboard importer (step 5 T6)."""

import json
from pathlib import Path

import pytest

from app.ops.import_dashboard import load_dashboards, plan_import, resolve_sources

DASH = {
    "name": "taas",
    "tiles": [
        {"name": "Throughput", "config": {"sourceId": "Job Events"}},
        {"name": "Queue depth", "config": {"sourceId": "Live Metrics"}},
        {"name": "Notes", "config": {"displayType": "markdown"}},
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
    assert [t["config"].get("sourceId") for t in resolved["tiles"]] == ["s1", "s2", None]
    assert DASH["tiles"][0]["config"]["sourceId"] == "Job Events"  # input untouched


def test_resolve_sources_names_what_is_missing() -> None:
    with pytest.raises(ValueError, match="Live Metrics"):
        resolve_sources(DASH, {"Job Events": "s1"})


CHART = Path(__file__).resolve().parent.parent / "deploy/helm/taas/files/clickstack"


def test_shipped_dashboard_fits_hyperdx_v2_and_the_chart_sources() -> None:
    """The fields HyperDX's v2 dashboards API requires (it rejected the first draft),
    and every tile's source one the chart creates (sources.json)."""
    sources = {s["name"] for s in json.loads((CHART / "sources.json").read_text())}
    (dashboard,) = load_dashboards((CHART / "dashboard.json").read_text())
    assert dashboard["tiles"]
    for tile in dashboard["tiles"]:
        assert {"name", "x", "y", "w", "h", "config"} <= set(tile), tile
        config = tile["config"]
        assert config["sourceId"] in sources, tile["name"]
        assert config["displayType"] and config["select"], tile["name"]
        for item in config["select"]:
            assert item["aggFn"] in {
                "avg",
                "count",
                "count_distinct",
                "max",
                "min",
                "quantile",
                "sum",
                "last_value",
                "any",
                "none",
            }
            assert item.get("whereLanguage") in (None, "sql"), tile["name"]
            assert "lucene" not in item.values(), tile["name"]
            if item["aggFn"] == "quantile":
                assert item["level"] in (0.5, 0.9, 0.95, 0.99), tile["name"]
