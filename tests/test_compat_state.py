"""Pins the compat request hash: what the shim writes it reads back, as Redis returns it."""

from compat.app.schemas import CompatRequest
from compat.app.state import CompatState


def test_reads_the_hash_as_redis_returns_it() -> None:
    raw = {b"engine": b"2", b"filenames": b'["a.jpg", "b.tif"]'}

    assert CompatRequest.from_hash(raw) == CompatRequest(engine=2, filenames=["a.jpg", "b.tif"])


async def test_request_round_trips_through_redis(redis_client) -> None:
    state = CompatState(redis_client, ttl=60)

    await state.create_request("req-1", engine=2, filenames=["a.jpg"])

    assert await state.get_request("req-1") == CompatRequest(engine=2, filenames=["a.jpg"])
    assert await state.get_request("unknown") is None
