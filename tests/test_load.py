"""Tests for the pure load arithmetic behind GET /load (step 4)."""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.config import Settings
from app.deps import require_load_token
from app.services.load import LoadTotals, compute_load, ewma, group_load


def test_ewma_first_sample_is_taken_as_is() -> None:
    assert ewma(None, 12.0) == 12.0


def test_ewma_moves_a_tenth_towards_the_sample() -> None:
    assert ewma(10.0, 20.0) == pytest.approx(11.0)
    assert ewma(10.0, 20.0, alpha=0.5) == pytest.approx(15.0)


# ---- compute_load / group_load ----

QUEUED = {"default|alto,txt": 4, "kramarky|alto,txt": 2, "maps|geojson,json": 1}
INFLIGHT = {"default|alto,txt": 3, "maps|geojson,json": 2}
SPP = {"default": 5.0, "maps": 60.0}
OCR_CAPS = {"default": frozenset({"alto", "txt"}), "kramarky": frozenset({"alto", "txt"})}
MAP_CAPS = {"maps": frozenset({"json", "geojson", "gcp"})}


@pytest.mark.parametrize(
    ("domains", "caps", "expected"),
    [
        # no filter: everything; queued + in-flight pages count; kramarky has no
        # measurement -> default 10 s
        (None, None, LoadTotals(7, 5, (4 + 3) * 5.0 + 2 * 10.0 + (1 + 2) * 60.0)),
        ({"default", "kramarky"}, None, LoadTotals(6, 3, 55.0)),
        ({"maps"}, None, LoadTotals(1, 2, 180.0)),
        (None, OCR_CAPS, LoadTotals(6, 3, 55.0)),
        (None, MAP_CAPS, LoadTotals(1, 2, 180.0)),
        # caps that lack an output the jobs want don't count them
        (None, {"default": frozenset({"alto"})}, LoadTotals(0, 0, 0.0)),
        # both filters at once
        ({"kramarky"}, OCR_CAPS, LoadTotals(2, 0, 20.0)),
        (None, {}, LoadTotals(0, 0, 0.0)),
    ],
)
def test_compute_load(domains, caps, expected) -> None:
    assert compute_load(QUEUED, INFLIGHT, SPP, 10.0, domains, caps) == expected


def test_in_flight_pages_keep_backlog_up_after_the_queue_empties() -> None:
    totals = compute_load({}, {"default|alto,txt": 3}, {"default": 5.0}, 10.0)
    assert totals == LoadTotals(0, 3, 15.0)


def test_negative_counters_count_as_zero() -> None:
    assert compute_load({"default|alto": -1}, {}, {}, 10.0) == LoadTotals(0, 0, 0.0)


def test_group_by_domain() -> None:
    groups = group_load(QUEUED, INFLIGHT, SPP, 10.0, "domain", {})
    assert groups == {
        "default": LoadTotals(4, 3, 35.0),
        "kramarky": LoadTotals(2, 0, 20.0),
        "maps": LoadTotals(1, 2, 180.0),
    }
    assert list(group_load(QUEUED, INFLIGHT, SPP, 10.0, "domain", {}, {"maps"})) == ["maps"]


def test_group_by_adapter_can_overlap() -> None:
    caps = {"tuzkaocr": OCR_CAPS, "tuzkam": MAP_CAPS, "both": {**OCR_CAPS, **MAP_CAPS}}
    groups = group_load(QUEUED, INFLIGHT, SPP, 10.0, "adapter", caps)
    assert groups["tuzkaocr"] == LoadTotals(6, 3, 55.0)
    assert groups["tuzkam"] == LoadTotals(1, 2, 180.0)
    assert groups["both"] == LoadTotals(7, 5, 235.0)


# ---- the token guard ----


@pytest.mark.asyncio
async def test_load_is_404_without_a_configured_token() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_load_token(None, Settings(load_token=""))
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "wrong"])
async def test_load_rejects_a_missing_or_wrong_token(token) -> None:
    credentials = (
        None if token is None else HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )
    with pytest.raises(HTTPException) as exc_info:
        await require_load_token(credentials, Settings(load_token="s3cret"))
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_load_accepts_the_token() -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="s3cret")
    await require_load_token(credentials, Settings(load_token="s3cret"))
