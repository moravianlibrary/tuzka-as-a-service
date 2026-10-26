"""Pins the job hash encoding: what JobState writes, it reads back, and it still reads the
hashes an older release wrote (bytes keys, empty strings for none, only a legacy fmt)."""

import json

import pytest
from pydantic import ValidationError

from app.schemas.redis import JobState
from app.vocabulary import JobStatus


def _state(**overrides) -> JobState:
    fields = {
        "username": "alice",
        "fmt": "multi",
        "outputs": frozenset({"alto", "txt"}),
        "submitted_at": 1750000000.5,
    }
    return JobState(**(fields | overrides))


def test_round_trip() -> None:
    state = _state(
        batch_id="b1",
        options={"lang": "ces"},
        megapixels=4.2,
        avoid_backends=frozenset({3, 1}),
        trace={"traceparent": "00-abc-def-01"},
    )

    assert JobState.from_hash(state.to_hash()) == state


def test_hash_encoding_matches_what_the_workers_read() -> None:
    stored = _state(options={"lang": "ces"}, megapixels=4.2, avoid_backends=frozenset({3, 1}))

    hash_fields = stored.to_hash()

    assert hash_fields["outputs"] == "alto,txt"
    assert hash_fields["options"] == '{"lang": "ces"}'
    assert hash_fields["mp"] == "4.2"  # the short field name the Lua and old pods read
    assert hash_fields["avoid_backends"] == "1,3"
    assert hash_fields["status"] == "queued"
    assert "external_id" not in hash_fields  # None is left out


def test_reads_a_hash_as_redis_returns_it() -> None:
    raw = {
        b"status": b"running",
        b"username": b"alice",
        b"external_id": b"",
        b"fmt": b"multi",
        b"outputs": b"alto,txt",
        b"options": b"{}",
        b"mp": b"2.5",
        b"submitted_at": b"1750000000.5",
        b"backend_id": b"7",
        b"avoid_backends": b"",
    }

    state = JobState.from_hash(raw)

    assert state.status is JobStatus.RUNNING
    assert state.external_id is None  # stored as "" for none
    assert state.backend_id == 7
    assert state.megapixels == 2.5
    assert state.avoid_backends == frozenset()


def test_legacy_hash_has_only_fmt() -> None:
    raw = {"username": "alice", "fmt": "alto", "submitted_at": "1750000000"}

    state = JobState.from_hash(raw)

    assert state.outputs == frozenset({"alto"})  # enqueued before outputs were stored
    assert state.domain == "default"


def test_hash_missing_submit_fields_is_rejected() -> None:
    raw = {"status": "running", "backend_id": "7"}  # recreated by a late write after TTL

    with pytest.raises(ValidationError):
        JobState.from_hash(raw)


def test_trace_context_is_stored_as_its_headers() -> None:
    state = _state(trace={"traceparent": "00-abc-def-01"})

    hash_fields = state.to_hash()

    assert hash_fields["traceparent"] == "00-abc-def-01"  # what older pods extract from
    assert "trace" not in hash_fields


_SUBMITTED = {"username": "alice", "fmt": "multi", "outputs": "alto,txt", "submitted_at": "1"}


@pytest.mark.parametrize(
    ("stored", "domain"),
    [({}, "default"), ({"domain": ""}, "default"), ({"domain": "kramarky"}, "kramarky")],
)
def test_domain_defaults_when_none_was_stored(stored, domain) -> None:
    assert JobState.from_hash(_SUBMITTED | stored).domain == domain


@pytest.mark.parametrize(
    ("stored", "outputs"),
    [
        ({"outputs": "alto,geojson", "fmt": "custom"}, {"alto", "geojson"}),
        ({"outputs": "", "fmt": "txt"}, {"txt"}),  # legacy: only fmt
        ({"outputs": "", "fmt": "multi"}, {"alto", "txt"}),
    ],
)
def test_outputs_come_from_the_hash_or_the_legacy_fmt(stored, outputs) -> None:
    assert JobState.from_hash(_SUBMITTED | stored).outputs == outputs


@pytest.mark.parametrize(
    ("stored", "options"),
    [
        (
            {"options": '{"alto_profile": "basic", "physical_img_nr": 3}'},
            {"alto_profile": "basic", "physical_img_nr": 3},
        ),
        ({"options": ""}, {}),
        ({}, {}),
    ],
)
def test_options_come_from_the_hash(stored, options) -> None:
    assert JobState.from_hash(_SUBMITTED | stored).options == options


@pytest.mark.parametrize(
    ("stored", "avoid"),
    [
        ({}, frozenset()),
        ({"avoid_backends": ""}, frozenset()),
        ({"avoid_backends": "5"}, frozenset({5})),
        ({"avoid_backends": "3,5,3,7"}, frozenset({3, 5, 7})),  # deduplicated
    ],
)
def test_avoid_backends_parse_to_ids(stored, avoid) -> None:
    assert JobState.from_hash(_SUBMITTED | stored).avoid_backends == avoid


def test_engine_run_is_stored_flat_as_older_releases_wrote_it() -> None:
    raw = dict(_SUBMITTED, harvest_times='{"n_lines": 42, "started_at": "2026-10-22T10:00:00"}')

    run = JobState.from_hash(raw).harvest_times

    assert run is not None
    assert run.started_at == "2026-10-22T10:00:00"
    assert run.metrics == {"n_lines": 42}
    stored = JobState.from_hash(raw).to_hash()["harvest_times"]
    assert json.loads(stored) == {  # flat again, timestamps beside the metrics
        "n_lines": 42,
        "created_at": None,
        "started_at": "2026-10-22T10:00:00",
        "finished_at": None,
    }
