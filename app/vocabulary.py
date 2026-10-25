"""The fixed words taas uses for states, stages and kinds (docs/typing.md).

One ``StrEnum`` per vocabulary that more than one module speaks. Members compare equal
to their strings and format as them, so values read back from Redis, Postgres or JSON
compare without conversion, and Redis, JSON and f-strings get the plain value.
"""

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    # The engine is done; the poller is saving the results.
    STORING = "storing"
    DONE = "done"
    FAILED = "failed"


# A job the reaper and batch completion still wait on.
ACTIVE_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.STORING})
TERMINAL_JOB_STATUSES = frozenset({JobStatus.DONE, JobStatus.FAILED})


class BatchStatus(StrEnum):
    # Pages are still being read in from the archive.
    OPEN = "open"
    # Every page is enqueued.
    QUEUED = "queued"
    # No page is left to run.
    FINISHED = "finished"


class EngineState(StrEnum):
    """A job's state as its engine reports it on the status endpoint."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: object) -> "EngineState":
        """The engine's status string; one taas doesn't know is UNKNOWN, which the poller
        keeps polling rather than failing the job over."""
        return cls(value) if isinstance(value, str) and value in cls else cls.UNKNOWN


class PollStatus(StrEnum):
    """What one status check found: the engine's state, or why there was none."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    # The engine reported an error, or the status check itself failed.
    ERROR = "error"
    UNKNOWN = "unknown"
    # The engine couldn't be reached (transport error).
    UNREACHABLE = "unreachable"


class PollAction(StrEnum):
    """What the poller does with a job after a status check."""

    HARVEST = "harvest"
    REQUEUE = "requeue"
    FAIL = "fail"
    # Not finished yet: poll it again later.
    RUNNING = "running"


class FailureStage(StrEnum):
    """Where a job's attempt failed (``jobs.attempts``, the event's ``error_class``)."""

    DISPATCH = "dispatch"
    # The status check failed (transport error, HTTP error from the status endpoint).
    POLL = "poll"
    # The engine reported the failure itself.
    ENGINE = "engine"
    HARVEST = "harvest"
    # The reaper failed a job that sat too long; never an attempt.
    TIMEOUT = "timeout"


class JobEventName(StrEnum):
    """The ``event`` of a ``taas.job_events`` row."""

    SUBMITTED = "submitted"
    DISPATCHED = "dispatched"
    RETRIED = "retried"
    DONE = "done"
    FAILED = "failed"


class ErrorClass(StrEnum):
    """What went wrong, coarse enough to chart (``taas.job_events.error_class``)."""

    NONE = ""
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    TRANSPORT = "transport"
    ENGINE_INPUT = "engine_input"
    ENGINE_INTERNAL = "engine_internal"
    TIMEOUT = "timeout"


class Device(StrEnum):
    """The hardware an engine runs on."""

    CPU = "cpu"
    GPU = "gpu"


class AdapterId(StrEnum):
    """Which engine adapter talks to a backend (``app.engines.ADAPTERS``)."""

    TUZKAOCR = "tuzkaocr"
    TUZKAM = "tuzkam"


class DomainSource(StrEnum):
    """Where a backend's capabilities for a domain came from."""

    # Reported by the engine at domain sync.
    ENGINE = "engine"
    # Declared at registration, until the engine is first seen.
    DECLARED = "declared"


class Fmt(StrEnum):
    """The legacy ``fmt`` a submit may give instead of ``outputs``."""

    ALTO = "alto"
    TXT = "txt"
    # ALTO and text.
    MULTI = "multi"


class LimitClass(StrEnum):
    """A rate-limited kind of request."""

    SUBMIT = "submit"
    QUERY = "query"
    WS_CONNECT = "ws_connect"


class LoadGroupBy(StrEnum):
    """How ``GET /load`` groups its totals."""

    DOMAIN = "domain"
    ADAPTER = "adapter"
