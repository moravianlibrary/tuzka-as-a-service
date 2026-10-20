"""Tests for read_limited: an upload is read in chunks and cut off past the limit."""

import pytest

from app.services.uploads import UploadTooLarge, read_limited


def _reader(data: bytes):
    reads: list[int] = []
    pos = 0

    async def read(size: int) -> bytes:
        nonlocal pos
        reads.append(size)
        chunk = data[pos : pos + size]
        pos += len(chunk)
        return chunk

    return read, reads


@pytest.mark.asyncio
async def test_reads_up_to_the_limit() -> None:
    read, _ = _reader(b"x" * 10)
    assert await read_limited(read, limit=10, chunk_size=4) == b"x" * 10


@pytest.mark.asyncio
async def test_aborts_at_limit_plus_one_without_reading_the_rest() -> None:
    read, reads = _reader(b"x" * 100)
    with pytest.raises(UploadTooLarge) as exc_info:
        await read_limited(read, limit=10, chunk_size=4)
    assert exc_info.value.limit == 10
    assert len(reads) == 3  # 4 + 4 + 4 bytes: past 10 after the third chunk, then stop


@pytest.mark.asyncio
async def test_empty_upload() -> None:
    read, _ = _reader(b"")
    assert await read_limited(read, limit=10) == b""
