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
