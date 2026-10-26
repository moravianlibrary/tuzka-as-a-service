"""What a poll of an in-flight job found, and what the poller does with a failed one."""

from dataclasses import dataclass
from datetime import datetime

from app.models.job import Job
from app.schemas.attempt import Attempt
from app.schemas.redis import EngineRun, JobState
from app.vocabulary import (
    FailureStage,
    PollStatus,
)


@dataclass(frozen=True, slots=True)
class PollOutcome:
    """What one status check of an in-flight job found, plus how to treat a failure."""

    job_id: str
    status: PollStatus
    state: JobState
    run: EngineRun
    retryable: bool  # a failure here may succeed on another backend
    http_status: int | None
    error: str | None
    # ENGINE when the engine reported the failure itself, POLL when the status check did.
    stage: FailureStage = FailureStage.POLL


@dataclass(frozen=True, slots=True)
class FailedPoll:
    """A polled job that fails for good: its error text and the attempt it records."""

    outcome: PollOutcome
    error: str
    attempt: Attempt


@dataclass(frozen=True, slots=True)
class RequeuedPoll:
    """A polled job that goes back on the queue after ``delay_s``, away from its backend."""

    outcome: PollOutcome
    delay_s: float
    attempt: Attempt


@dataclass(frozen=True, slots=True)
class OutputStats:
    """What a harvested job's outputs say about the page, for its done event."""

    lines: int | None
    blocks: int | None
    chars: int | None
    mean_conf: float | None


@dataclass(frozen=True, slots=True)
class StoredHarvest:
    """A harvest's stored results: whether it was the first to mark the job done, the
    job row as stored, and its timeline (engine clock, then the taas-clock store)."""

    first_harvest: bool
    job: Job | None
    device: str
    state_ttl: int
    urls: dict[str, str]  # output → presigned URL
    engine_received_at: datetime | None
    started_at: datetime | None
    engine_finished_at: datetime | None
    finished_at: datetime  # the engine's finish, else when the poller saw it
    stored_at: datetime

    @property
    def run_seconds(self) -> float | None:
        """The engine's run time, when it reported both ends of it."""
        if self.started_at is None or self.engine_finished_at is None:
            return None
        return (self.engine_finished_at - self.started_at).total_seconds()
