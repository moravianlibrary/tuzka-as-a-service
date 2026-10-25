"""Engine adapter protocol and shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.vocabulary import EngineState


@dataclass(frozen=True)
class DomainInfo:
    outputs: frozenset[str]
    # JSON Schema of the per-job options; None = the engine didn't describe any.
    options_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class EngineInfo:
    domains: dict[str, DomainInfo]  # empty = engine advertised nothing
    version: str | None


@dataclass(frozen=True)
class EngineStatus:
    state: EngineState
    error: str | None = None
    error_status: int | None = None
    created_at: str | None = None  # engine ISO strings, parsed by the poller
    started_at: str | None = None
    finished_at: str | None = None
    # n_lines, mean_conf, ...; adapter-specific ones too (TuzkaM: georef.status, ...)
    metrics: dict[str, int | float | str] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    content: bytes
    content_type: str


class EngineFullError(Exception):
    pass


class OutputUnavailable(Exception):
    """The engine finished the job but can't produce this output for it (e.g. a map
    whose georeferencing failed has no ground control points). Not retryable: another
    attempt gives the same answer."""

    def __init__(self, output: str, reason: str) -> None:
        super().__init__(f"{output} unavailable: {reason}")
        self.output = output
        self.reason = reason


class EngineAdapter(Protocol):
    async def healthy(self) -> bool: ...
    async def describe(self) -> EngineInfo: ...
    async def submit(
        self,
        image: bytes,
        filename: str,
        domain: str | None,
        outputs: frozenset[str],
        options: dict[str, Any],
    ) -> str: ...
    async def status(self, engine_job_id: str) -> EngineStatus: ...
    async def fetch(self, engine_job_id: str, output: str) -> Result: ...
    async def version(self) -> str | None: ...
    @classmethod
    def validate_request(
        cls, domain: str, outputs: frozenset[str], options: dict[str, Any]
    ) -> str | None:
        """A reason to reject the request at submit, for checks across fields that a
        JSON Schema can't express; None if it's fine. Called without an instance (the
        API holds no adapters), so it can't ask the engine."""
        ...
