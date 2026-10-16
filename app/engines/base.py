"""Engine adapter protocol and shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pydantic import BaseModel


@dataclass(frozen=True)
class DomainInfo:
    outputs: frozenset[str]


@dataclass(frozen=True)
class EngineInfo:
    domains: dict[str, DomainInfo]  # empty = engine advertised nothing
    version: str | None


@dataclass(frozen=True)
class EngineStatus:
    state: str  # queued | running | done | failed | error | unknown
    error: str | None = None
    error_status: int | None = None
    created_at: str | None = None  # engine ISO strings, parsed by the poller
    started_at: str | None = None
    finished_at: str | None = None
    metrics: dict[str, int | float] = field(default_factory=dict)  # n_lines, mean_conf, ...


@dataclass(frozen=True)
class Result:
    content: bytes
    content_type: str


class EngineFullError(Exception):
    pass


class EngineAdapter(Protocol):
    async def healthy(self) -> bool: ...
    async def describe(self) -> EngineInfo: ...
    async def submit(
        self,
        image: bytes,
        filename: str,
        domain: str | None,
        outputs: frozenset[str],
        options: BaseModel,
    ) -> str: ...
    async def status(self, engine_job_id: str) -> EngineStatus: ...
    async def fetch(self, engine_job_id: str, output: str) -> Result: ...
    def options_schema(self, domain: str) -> type[BaseModel]: ...
    async def version(self) -> str | None: ...
