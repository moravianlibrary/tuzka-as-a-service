"""The Redis hashes taas keeps per job, parsed once where they're read (docs/typing.md).

A hash stores every value as a string: sets as comma-separated values, options and the
engine's last status as JSON, and an empty string for "none". ``from_hash`` and
``to_hash`` keep exactly that encoding, so hashes written by an older release stay
readable during a rolling upgrade.
"""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.vocabulary import DEFAULT_DOMAIN, Fmt, JobStatus

# Hash fields holding comma-separated values or JSON.
_CSV_FIELDS = frozenset({"outputs", "avoid_backends"})
_JSON_FIELDS = frozenset({"options", "harvest_times"})
# W3C trace context headers, stored as top-level hash fields.
_TRACE_FIELDS = frozenset({"traceparent", "tracestate", "baggage"})


class JobState(BaseModel):
    """A job's ``job:<id>`` hash: what the submit wrote, plus what dispatch, polling and
    harvesting have recorded since."""

    model_config = ConfigDict(frozen=True)

    status: JobStatus = JobStatus.QUEUED
    username: str
    # The caller's own label; None when they gave none.
    external_id: str | None = None
    batch_id: str | None = None
    # jobs.fmt: the legacy fmt of the outputs, or "custom".
    fmt: str
    domain: str = DEFAULT_DOMAIN
    outputs: frozenset[str]
    # Engine options, validated against the domain's schema at submit.
    options: dict[str, Any] = Field(default_factory=dict)
    # The uploaded image's extension, which names its object in storage.
    ext: str = ".jpg"
    # Stored as "mp", the name the Lua scripts and older releases read.
    megapixels: float = Field(
        default=1.0, validation_alias=AliasChoices("megapixels", "mp"), serialization_alias="mp"
    )
    # Unix time; also the job's score in its pending queue.
    submitted_at: float
    priority: int = 0
    # W3C trace context of the submit request (traceparent, …), for the workers to
    # continue its trace; empty when tracing is off.
    trace: dict[str, str] = Field(default_factory=dict)

    # Dispatch: when a submit worker claimed it, and what a delayed retry needs.
    claimed_at: float | None = None
    queue_score: float | None = None
    avoid_backends: frozenset[int] = frozenset()
    requeues: int = 0

    # Running on an engine.
    engine_job_id: str | None = None
    backend_url: str | None = None
    backend_id: int | None = None
    next_poll_at: float | None = None
    last_poll: float | None = None

    # Storing: the engine's last status, and failed harvest attempts so far.
    harvest_times: dict[str, Any] | None = None
    harvest_attempts: int = 0

    # Finished.
    error: str | None = None
    finished_at: float | None = None

    @classmethod
    def from_hash(cls, raw: Mapping[Any, Any]) -> "JobState":
        """Parse an HGETALL reply. Raises ``pydantic.ValidationError`` for a hash that
        lacks what every submit writes (e.g. one recreated by a late write after its
        TTL expired)."""
        fields: dict[str, Any] = {}
        for key, value in raw.items():
            name, text = _text(key), _text(value)
            if text == "":
                continue  # how the writers store "none"
            if name in _TRACE_FIELDS:
                fields.setdefault("trace", {})[name] = text
            elif name in _CSV_FIELDS:
                fields[name] = [item for item in text.split(",") if item.strip()]
            elif name in _JSON_FIELDS:
                fields[name] = json.loads(text)
            else:
                fields[name] = text
        if "outputs" not in fields and "fmt" in fields:
            # Enqueued by a release that stored only the legacy fmt.
            fields["outputs"] = Fmt(fields["fmt"]).outputs
        return cls.model_validate(fields)

    def to_hash(self) -> dict[str, str]:
        """The fields to HSET, in the encoding ``from_hash`` reads; None is left out."""
        dumped = self.model_dump(by_alias=True, exclude_none=True, exclude={"trace"})
        fields = {name: _encode(value) for name, value in dumped.items()}
        return fields | {name: value for name, value in self.trace.items() if name in _TRACE_FIELDS}


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _encode(value: Any) -> str:
    if isinstance(value, set | frozenset):
        return ",".join(sorted(str(item) for item in value))
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return str(value)
