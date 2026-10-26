"""What a poll of an in-flight job found, and what the poller does with a failed one."""

from dataclasses import dataclass

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
