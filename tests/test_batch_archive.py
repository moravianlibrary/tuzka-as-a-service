"""Tests for a batch's results archive (step 7 T6): the manifest (pure) and a round trip
through the builder with storage faked in memory."""

import io
import json
import tarfile
import uuid

import pytest
import zstandard

from app.services import batch_archive
from app.services.batch_archive import PageRow, build_manifest, page_path

J1, J2 = uuid.uuid4(), uuid.uuid4()
ROWS = [
    PageRow(J1, "scans/p1.tif", "done", None, ["alto", "txt"]),
    PageRow(J2, "scans/p2.tif", "failed", "engine error", []),
]


def test_page_paths() -> None:
    assert page_path("scans/p1.tif", "alto") == "pages/scans/p1.xml"
    assert page_path("p1.jpg", "txt") == "pages/p1.txt"
    assert page_path("../../etc/p.jpg", "alto") == "pages/etc/p.xml"  # stays inside


def test_manifest_lists_every_page_and_failed_ones_have_no_outputs() -> None:
    manifest = build_manifest(ROWS)
    assert manifest[0] == {
        "source_name": "scans/p1.tif",
        "job_id": str(J1),
        "status": "done",
        "outputs": {"alto": "pages/scans/p1.xml", "txt": "pages/scans/p1.txt"},
        "error": None,
    }
    assert manifest[1]["outputs"] == {}
    assert manifest[1]["error"] == "engine error"


@pytest.mark.asyncio
async def test_round_trip(monkeypatch) -> None:
    cctx = zstandard.ZstdCompressor()
    stored = {
        f"alice/{J1}.xml.zst": cctx.compress(b"<alto/>"),
        f"alice/{J1}.txt.zst": cctx.compress(b"text"),
    }
    uploaded: dict[str, bytes] = {}

    async def fake_get(client, bucket, path):
        return stored[path]

    async def fake_put(client, bucket, path, f, length, content_type):
        uploaded[path] = f.read()
        assert len(uploaded[path]) == length

    monkeypatch.setattr(batch_archive, "get_object", fake_get)
    monkeypatch.setattr(batch_archive, "put_file", fake_put)

    size = await batch_archive.build_results_archive(ROWS, "alice", None, "results", "out")

    assert size == len(uploaded["out"])
    raw = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(uploaded["out"]))
    with tarfile.open(fileobj=raw, mode="r|") as tar:
        files = {m.name: tar.extractfile(m).read() for m in tar if m.isreg()}
    assert files["pages/scans/p1.xml"] == b"<alto/>"
    assert files["pages/scans/p1.txt"] == b"text"
    assert json.loads(files["manifest.json"])[1]["status"] == "failed"
