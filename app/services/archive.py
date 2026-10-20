"""Batch archives (step 7): which kind an upload is, and its members one at a time.

The kind comes from magic bytes, never the file name (D2). Members are read in stream
mode where the format allows it, so a 2 GiB archive never sits in memory: one member at
a time, and only its own bytes (D4). Synchronous on purpose: the router runs it in a
worker thread while it uploads each member.
"""

from __future__ import annotations

import tarfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO, Literal

import zstandard

ArchiveKind = Literal["tar", "tar.gz", "tar.zst", "zip"]

# detect_archive needs this many bytes: tar's "ustar" magic sits at offset 257.
HEAD_BYTES = 262

_ZSTD = b"\x28\xb5\x2f\xfd"
_GZIP = b"\x1f\x8b"
_ZIP = b"PK\x03\x04"


@dataclass(frozen=True)
class Member:
    name: str  # path inside the archive, as the client named it
    size: int
    data: bytes


class MemberTooLarge(Exception):
    def __init__(self, name: str, size: int, limit: int) -> None:
        super().__init__(f"{name}: {size} bytes, over the {limit}-byte page limit")
        self.name = name


def detect_archive(head: bytes) -> ArchiveKind | None:
    """The archive kind from its first ``HEAD_BYTES`` bytes, or None if it's none of
    the four."""
    if head.startswith(_ZSTD):
        return "tar.zst"
    if head.startswith(_GZIP):
        return "tar.gz"
    if head.startswith(_ZIP):
        return "zip"
    if head[257:262] == b"ustar":
        return "tar"
    return None


def iter_members(f: BinaryIO, kind: ArchiveKind, max_member_bytes: int) -> Iterator[Member]:
    """Yield the archive's regular files, in archive order. Directories, links and
    special files are skipped. A member over ``max_member_bytes`` raises
    ``MemberTooLarge`` before its bytes are read."""
    if kind == "zip":
        yield from _zip_members(f, max_member_bytes)
        return
    if kind == "tar.zst":
        with zstandard.ZstdDecompressor().stream_reader(f, closefd=False) as zst:
            yield from _tar_members(zst, "r|", max_member_bytes)
        return
    yield from _tar_members(f, "r|gz" if kind == "tar.gz" else "r|", max_member_bytes)


def _tar_members(f: BinaryIO, mode: str, limit: int) -> Iterator[Member]:
    # Stream mode: members must be read in order, and each one before the next.
    with tarfile.open(fileobj=f, mode=mode) as tar:  # type: ignore[call-overload]
        for info in tar:
            if not info.isreg():
                continue
            if info.size > limit:
                raise MemberTooLarge(info.name, info.size, limit)
            extracted = tar.extractfile(info)
            if extracted is None:
                continue
            yield Member(info.name, info.size, extracted.read())


def _zip_members(f: BinaryIO, limit: int) -> Iterator[Member]:
    # zip keeps its index at the end, so it needs a seekable file (the spooled upload is).
    with zipfile.ZipFile(f) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.file_size > limit:
                raise MemberTooLarge(info.filename, info.file_size, limit)
            yield Member(info.filename, info.file_size, archive.read(info))
