"""The values dispatch allocates: a job waiting to be dispatched, and a backend's slots."""

from dataclasses import dataclass, field
from typing import Any

from app.models.backend import Backend
from app.services.events import failure
from app.vocabulary import FailureStage


@dataclass(frozen=True)
class PendingJob:
    """A job waiting to be dispatched, carrying its avoid-list from prior failures."""

    job_id: str
    score: float
    domain: str
    outputs: frozenset[str]
    avoid: frozenset[int] = field(default_factory=frozenset)
    megapixels: float = 1.0
    batch_id: str | None = None


@dataclass
class BackendSlot:
    """A healthy backend, its remaining free capacity (may be 0) and what it serves:
    domain → the outputs it produces for that domain. For scoring: its pages in flight,
    the batches among them (batch id → pages), and what this tick assigned it."""

    backend: Backend
    free: int
    domains: dict[str, frozenset[str]]
    inflight: int = 0
    batches: dict[str, int] = field(default_factory=dict)
    assigned: int = 0


@dataclass(frozen=True, slots=True)
class DispatchFailure:
    """Why a dispatch failed, and whether another backend might succeed."""

    error: str
    http_status: int | None
    transient: bool  # worth retrying elsewhere while the requeue budget lasts
    transport: bool  # the engine wasn't reached at all

    @property
    def event_fields(self) -> dict[str, Any]:
        """The error columns of the job's ``retried``/``failed`` event."""
        return failure(FailureStage.DISPATCH, self.http_status, self.transport)
