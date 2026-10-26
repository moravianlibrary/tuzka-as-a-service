"""Request building, response parsing and archive handling shared by both clients."""

from __future__ import annotations

import json
import os
import tarfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import zstandard

from .models import BatchPage, BatchProgress, BatchStatus, BatchSubmit, DomainInfo

# What each legacy fmt asks for; batches only take outputs.
FMT_OUTPUTS = {"multi": ("alto", "txt"), "alto": ("alto",), "txt": ("txt",)}

MANIFEST_NAME = "manifest.json"


def job_form(
    external_id: str | None,
    fmt: str,
    domain: str | None,
    outputs: Iterable[str] | None,
    options: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Form fields for ``POST /api/v1/jobs``. ``fmt`` is only sent without ``outputs``."""
    data: dict[str, str] = {}
    if external_id is not None:
        data["external_id"] = external_id
    if outputs is not None:
        data["outputs"] = ",".join(outputs)
    else:
        data["fmt"] = fmt
    if domain:
        data["domain"] = domain
    if options:
        data["options"] = json.dumps(dict(options))
    return data


def batch_form(
    external_id: str | None,
    fmt: str,
    domain: str | None,
    outputs: Iterable[str] | None,
    options: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Form fields for ``POST /api/v1/batches``, which takes outputs only."""
    data = {"outputs": ",".join(outputs if outputs is not None else FMT_OUTPUTS[fmt])}
    if external_id is not None:
        data["external_id"] = external_id
    if domain:
        data["domain"] = domain
    if options:
        data["options"] = json.dumps(dict(options))
    return data


# ---- responses ----


def parse_domains(body: dict[str, Any]) -> list[DomainInfo]:
    return [
        DomainInfo(
            name=d["name"],
            label=d.get("label"),
            description=d.get("description"),
            outputs=list(d.get("outputs", [])),
            options_schema=d.get("options_schema"),
            legacy_engine_id=d.get("legacy_engine_id"),
        )
        for d in body.get("domains", [])
    ]


def parse_batch_submit(body: dict[str, Any]) -> BatchSubmit:
    return BatchSubmit(
        batch_id=UUID(body["batch_id"]),
        external_id=body.get("external_id"),
        total=int(body["total"]),
        skipped=list(body.get("skipped", [])),
    )


def parse_batch_status(body: dict[str, Any]) -> BatchStatus:
    pages = body.get("pages")
    return BatchStatus(
        batch_id=UUID(body["batch_id"]),
        external_id=body.get("external_id"),
        status=body["status"],
        domain=body.get("domain"),
        outputs=list(body.get("outputs") or []),
        total=int(body["total"]),
        queued=int(body.get("queued", 0)),
        running=int(body.get("running", 0)),
        done=int(body.get("done", 0)),
        failed=int(body.get("failed", 0)),
        submitted_at=body.get("submitted_at"),
        finished_at=body.get("finished_at"),
        eta_s=body.get("eta_s"),
        pages=None
        if pages is None
        else [
            BatchPage(
                source_name=p["source_name"],
                job_id=UUID(p["job_id"]),
                status=p["status"],
                error=p.get("error"),
            )
            for p in pages
        ],
    )


def parse_batch_event(event: dict[str, Any]) -> BatchProgress | None:
    """A WebSocket batch progress event, or None if ``event`` is something else."""
    if event.get("type") != "batch":
        return None
    return BatchProgress(
        batch_id=UUID(event["batch_id"]),
        total=int(event["total"]),
        done=int(event["done"]),
        failed=int(event["failed"]),
    )


def is_page_event(event: dict[str, Any]) -> bool:
    """A per-page job event this client fetches results for. Pages of a batch are left
    out: a batch's results are downloaded as one archive."""
    return "job_id" in event and "status" in event and not event.get("batch_id")


# ---- archives ----

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"


def is_archive(path: Path) -> bool:
    """Whether ``path`` is an archive the batch API accepts (same magic bytes it checks)."""
    with path.open("rb") as f:
        head = f.read(262)
    return head.startswith((_ZSTD_MAGIC, _GZIP_MAGIC, _ZIP_MAGIC)) or head[257:262] == b"ustar"


def collect_images(source: Path | Iterable[Path | str]) -> list[tuple[Path, str]]:
    """``(path, name in the archive)`` for a directory (recursively, names relative to
    it) or a list of files (names are the file names)."""
    if isinstance(source, Path):
        files = sorted(p for p in source.rglob("*") if p.is_file())
        return [(p, p.relative_to(source).as_posix()) for p in files]
    return [(Path(p), Path(p).name) for p in source]


def pack_tar_zst(images: list[tuple[Path, str]], dest: Path) -> None:
    """Write ``images`` to ``dest`` as a ``.tar.zst``, streaming one file at a time."""
    with dest.open("wb") as raw:
        with zstandard.ZstdCompressor().stream_writer(raw, closefd=False) as zst:
            with tarfile.open(fileobj=zst, mode="w|") as tar:
                for path, name in images:
                    tar.add(path, arcname=name, recursive=False)


def extract_tar_zst(archive: Path, dest: Path) -> list[dict[str, Any]] | None:
    """Unpack a ``.tar.zst`` into ``dest``; returns its manifest if it has one."""
    dest.mkdir(parents=True, exist_ok=True)
    with archive.open("rb") as raw:
        with zstandard.ZstdDecompressor().stream_reader(raw) as zst:
            with tarfile.open(fileobj=zst, mode="r|") as tar:
                # The "data" filter refuses absolute paths, .. and links out of dest.
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(dest, filter="data")
                else:  # Python < 3.11.4
                    tar.extractall(dest)
    manifest = dest / MANIFEST_NAME
    if not manifest.is_file():
        return None
    entries: list[dict[str, Any]] = json.loads(manifest.read_text())
    return entries


def batch_paths(dest: Path | str, batch_id: UUID) -> tuple[Path, Path]:
    """Where a batch's results archive is saved and extracted under ``dest``."""
    root = Path(dest)
    os.makedirs(root, exist_ok=True)
    return root / f"{batch_id}.tar.zst", root / str(batch_id)
