"""A batch's results archive (step 7, D6): every page's outputs and a manifest.

Built once, when the batch has finished (or on demand for ``?partial=1``), streamed
page by page: each stored output is downloaded, decompressed, added to a tar that is
zstd-compressed into a spooled temp file (memory up to 64 MiB, then disk), and the
whole is uploaded to the results bucket. No more than one page is in memory at a time.
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import zstandard
from miniopy_async.api import Minio

from app.engines.outputs import ext_for
from app.services.storage import get_object, put_file

MANIFEST = "manifest.json"
SPOOL_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class PageRow:
    job_id: uuid.UUID
    source_name: str
    status: str
    error: str | None
    outputs: list[str]  # the outputs stored for it (empty unless done)


def page_path(source_name: str, output: str) -> str:
    """Where a page's output goes in the archive: ``pages/<its path, minus extension>.<ext>``,
    so ``scans/p1.tif`` with ``alto`` is ``pages/scans/p1.xml``."""
    stem = PurePosixPath(source_name).with_suffix("")
    # A path that tries to leave the archive root is kept inside it.
    safe = PurePosixPath(*[p for p in stem.parts if p not in ("", ".", "..", "/")])
    return f"pages/{safe}.{ext_for(output)}"


def build_manifest(rows: list[PageRow]) -> list[dict[str, Any]]:
    """One entry per page, in the order given: what it was called in the archive, its
    job, its status, and where each of its outputs is (or the error, if it failed)."""
    return [
        {
            "source_name": row.source_name,
            "job_id": str(row.job_id),
            "status": row.status,
            "outputs": {o: page_path(row.source_name, o) for o in sorted(row.outputs)},
            "error": row.error,
        }
        for row in rows
    ]


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


async def build_results_archive(
    rows: list[PageRow],
    username: str,
    results_client: Minio,
    bucket: str,
    object_path: str,
) -> int:
    """Build the archive of ``rows`` and upload it to ``object_path``; returns its size."""
    dctx = zstandard.ZstdDecompressor()
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES) as spool:
        with zstandard.ZstdCompressor().stream_writer(spool, closefd=False) as zst:
            with tarfile.open(fileobj=zst, mode="w|") as tar:
                for row in rows:
                    for output in sorted(row.outputs):
                        stored = f"{username}/{row.job_id}.{ext_for(output)}.zst"
                        data = await get_object(results_client, bucket, stored)
                        _add(tar, page_path(row.source_name, output), dctx.decompress(data))
                manifest = json.dumps(build_manifest(rows), indent=2, ensure_ascii=False)
                _add(tar, MANIFEST, manifest.encode())
        size = spool.tell()
        spool.seek(0)
        await put_file(results_client, bucket, object_path, spool, size, "application/zstd")
    return size
