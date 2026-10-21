"""Reading an upload with a size limit, without holding more than the limit in memory."""

from collections.abc import Awaitable, Callable

UPLOAD_CHUNK_BYTES = 1024 * 1024


class UploadTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"File too large. Max: {limit} bytes")
        self.limit = limit


async def read_limited(
    read: Callable[[int], Awaitable[bytes]], limit: int, chunk_size: int = UPLOAD_CHUNK_BYTES
) -> bytes:
    """Read ``read`` (e.g. ``UploadFile.read``) chunk by chunk, stopping as soon as the
    data passes ``limit`` rather than after loading it all."""
    data = bytearray()
    while chunk := await read(chunk_size):
        data += chunk
        if len(data) > limit:
            raise UploadTooLarge(limit)
    return bytes(data)
