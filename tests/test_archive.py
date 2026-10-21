"""Tests for batch archive detection and member iteration (step 7 T1). Pure: the
archives are built in memory."""

import io
import tarfile
import zipfile

import pytest
import zstandard

from app.services.archive import (
    HEAD_BYTES,
    MemberTooLarge,
    detect_archive,
    iter_members,
)

FILES = {"pages/p1.jpg": b"\xff\xd8 one", "pages/p2.jpg": b"\xff\xd8 two", "notes.txt": b"hi"}


def _tar(mode: str = "w") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        directory = tarfile.TarInfo("pages")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        for name, data in FILES.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("link.jpg")
        link.type = tarfile.SYMTYPE
        link.linkname = "pages/p1.jpg"
        tar.addfile(link)
    return buf.getvalue()


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("pages/", b"")
        for name, data in FILES.items():
            archive.writestr(name, data)
    return buf.getvalue()


ARCHIVES = {
    "tar": _tar(),
    "tar.gz": _tar("w:gz"),
    "tar.zst": zstandard.ZstdCompressor().compress(_tar()),
    "zip": _zip(),
}


@pytest.mark.parametrize("kind", sorted(ARCHIVES))
def test_detects_each_kind(kind) -> None:
    assert detect_archive(ARCHIVES[kind][:HEAD_BYTES]) == kind


def test_garbage_is_not_an_archive() -> None:
    assert detect_archive(b"\xff\xd8\xff\xe0 just a jpeg" + b"\0" * 300) is None
    assert detect_archive(b"") is None


@pytest.mark.parametrize("kind", sorted(ARCHIVES))
def test_yields_regular_files_only(kind) -> None:
    members = list(iter_members(io.BytesIO(ARCHIVES[kind]), kind, 1024))
    # The .txt is still yielded: the extension check is the router's job.
    assert {m.name: m.data for m in members} == FILES
    assert all(m.size == len(m.data) for m in members)


@pytest.mark.parametrize("kind", sorted(ARCHIVES))
def test_oversize_member_raises(kind) -> None:
    with pytest.raises(MemberTooLarge) as exc_info:
        list(iter_members(io.BytesIO(ARCHIVES[kind]), kind, 4))
    assert exc_info.value.name in FILES
