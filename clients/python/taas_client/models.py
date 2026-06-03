from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class JobEvent:
    uuid: UUID
    status: JobStatus
    alto_url: str | None = None
    txt_url: str | None = None
    error: str | None = None
    ts: str | None = None


@dataclass
class JobResult:
    uuid: UUID
    alto: bytes | None = None
    txt: bytes | None = None
