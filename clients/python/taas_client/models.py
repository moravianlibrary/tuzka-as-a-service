from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STORING = "storing"
    DONE = "done"
    FAILED = "failed"


@dataclass
class JobEvent:
    job_id: UUID
    status: JobStatus
    # Your label from submit(external_id=...), echoed back; None if you sent none.
    external_id: str | None = None
    alto_url: str | None = None
    txt_url: str | None = None
    error: str | None = None
    ts: str | None = None


@dataclass
class JobResult:
    job_id: UUID
    external_id: str | None = None
    alto: bytes | None = None
    txt: bytes | None = None


@dataclass(frozen=True)
class DomainInfo:
    """A domain you can submit to (``GET /api/v1/domains``)."""

    name: str
    label: str | None
    description: str | None
    outputs: list[str]
    # JSON Schema of the ``options`` this domain accepts; None = none yet.
    options_schema: dict[str, Any] | None
    legacy_engine_id: int | None


@dataclass(frozen=True)
class BatchSubmit:
    batch_id: UUID
    external_id: str | None
    total: int
    # Archive members that weren't images and were left out.
    skipped: list[str]


@dataclass(frozen=True)
class BatchPage:
    source_name: str  # the page's path inside the archive you sent
    job_id: UUID
    status: str
    error: str | None = None


@dataclass(frozen=True)
class BatchStatus:
    batch_id: UUID
    external_id: str | None
    status: str  # open | queued | finished
    domain: str | None
    outputs: list[str]
    total: int
    queued: int
    running: int
    done: int
    failed: int
    submitted_at: str | None = None
    finished_at: str | None = None
    eta_s: float | None = None  # a rough estimate from the server
    pages: list[BatchPage] | None = None

    @property
    def finished(self) -> bool:
        return self.status == "finished"


@dataclass(frozen=True)
class BatchProgress:
    """A batch progress event pushed over the WebSocket."""

    batch_id: UUID
    total: int
    done: int
    failed: int

    @property
    def finished(self) -> bool:
        return self.done + self.failed >= self.total


@dataclass(frozen=True)
class BatchResults:
    archive: Path  # the downloaded .tar.zst
    directory: Path | None = None  # where it was extracted, if it was
    # manifest.json: one entry per page (source_name, status, outputs, error, ...)
    manifest: list[dict[str, Any]] | None = None
