"""What a submitted job asks for — domain, outputs, options — and its validation (D7).

The parsers and ``options_errors`` are pure; ``resolve_job_spec`` adds the one DB read
of what the enabled backends serve and the domain's stored options schema. No engine
adapter is involved: everything is checked against what domain sync recorded.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines import ADAPTERS
from app.exceptions import Unprocessable
from app.models.backend import Backend
from app.models.backend_domain import BackendDomain
from app.models.domain import Domain
from app.vocabulary import DEFAULT_DOMAIN, AdapterId, Fmt

# How long a domain's capabilities (outputs per enabled backend + options schema) are
# reused across submits before the DB is asked again. Short: a backend disabled or
# re-synced shows up within seconds, and the dispatcher re-checks capabilities anyway.
CAPABILITIES_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class BackendCapability:
    """What one enabled backend does for a domain: its outputs, the domain's options
    schema (the same on every backend), and the backend's adapter."""

    outputs: frozenset[str]
    options_schema: dict[str, Any] | None
    adapter: AdapterId


@dataclass(frozen=True)
class _CachedCapabilities:
    loaded_at: float  # time.monotonic()
    backends: list[BackendCapability]


_capabilities: dict[str, _CachedCapabilities] = {}


@dataclass(frozen=True)
class JobSpec:
    domain: str
    outputs: frozenset[str]
    options: dict[str, Any]


def parse_outputs(raw: str | None, fmt: Fmt) -> frozenset[str]:
    """The ``outputs`` form field (comma-separated) as a set; ``fmt`` decides when it's
    absent. Raises ``ValueError`` on an empty item or an empty set."""
    if raw is None:
        return fmt.outputs
    items = [item.strip().lower() for item in raw.split(",")]
    if any(not item for item in items):
        raise ValueError("outputs must be a comma-separated list of output names")
    return frozenset(items)


EXTERNAL_ID_MAX = 255


def parse_external_id(raw: str | None) -> str | None:
    """The caller's ``external_id`` label: optional, 1–255 printable characters, never
    parsed (a PID like ``uuid:…``, a signature, a file name). Raises ``ValueError``."""
    if raw is None or raw == "":
        return None
    if len(raw) > EXTERNAL_ID_MAX:
        raise ValueError(f"external_id is longer than {EXTERNAL_ID_MAX} characters")
    if not raw.isprintable():
        raise ValueError("external_id must be printable text")
    return raw


def parse_options(raw: str | None) -> dict[str, Any]:
    """The ``options`` form field: a JSON object, ``{}`` when absent. Raises ``ValueError``."""
    if raw is None or not raw.strip():
        return {}
    try:
        options = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"options must be a JSON object: {exc.msg}") from exc
    if not isinstance(options, dict):
        raise ValueError("options must be a JSON object")
    return options


def fmt_label(outputs: frozenset[str]) -> str:
    """``jobs.fmt`` for a set of outputs: its legacy fmt, or ``custom`` if it has none.
    Analytics and the dashboard still read ``fmt``."""
    try:
        return Fmt.from_outputs(outputs)
    except ValueError:
        return "custom"


@lru_cache(maxsize=64)
def _validator(schema_json: str) -> Draft202012Validator:
    """A compiled validator per distinct schema. Schemas change only on domain sync, so a
    handful of entries covers every domain; keyed by the canonical JSON, a changed schema
    simply gets a new entry."""
    return Draft202012Validator(json.loads(schema_json))


def options_errors(schema: dict[str, Any] | None, options: dict[str, Any]) -> list[str]:
    """Why ``options`` don't satisfy ``schema``; empty when they do. A domain with no
    stored schema yet accepts only ``{}``."""
    if schema is None:
        return [f"unknown option '{key}'" for key in sorted(options)]
    messages = []
    validator = _validator(json.dumps(schema, sort_keys=True))
    for error in validator.iter_errors(options):
        where = ".".join(str(p) for p in error.absolute_path)
        messages.append(f"{where}: {error.message}" if where else error.message)
    return sorted(messages)


def adapter_rejection(
    adapters: set[AdapterId], domain: str, outputs: frozenset[str], options: dict[str, Any]
) -> str | None:
    """The engine-specific checks a schema can't express (e.g. TuzkaM: GeoJSON needs
    georef). The request is fine if any adapter that could take it accepts it; else the
    first adapter's reason."""
    reasons = []
    for adapter in sorted(adapters):
        reason = ADAPTERS[adapter].validate_request(domain, outputs, options)
        if reason is None:
            return None
        reasons.append(reason)
    return reasons[0] if reasons else None


def clear_capabilities_cache() -> None:
    _capabilities.clear()


async def _domain_capabilities(db: AsyncSession, name: str) -> list[BackendCapability]:
    """Each enabled backend serving ``name``, cached for ``CAPABILITIES_TTL_SECONDS``. Only served domains are cached, so arbitrary names in
    requests can't grow the cache."""
    now = time.monotonic()
    hit = _capabilities.get(name)
    if hit is not None and now - hit.loaded_at < CAPABILITIES_TTL_SECONDS:
        return hit.backends
    result = await db.execute(
        select(BackendDomain.outputs, Domain.options_schema, Backend.adapter)
        .join(Domain, Domain.id == BackendDomain.domain_id)
        .join(Backend, Backend.id == BackendDomain.backend_id)
        .where(Domain.name == name, Backend.enabled.is_(True))
    )
    backends = [
        BackendCapability(frozenset(outputs), schema, adapter)
        for outputs, schema, adapter in result.all()
    ]
    if backends:
        _capabilities[name] = _CachedCapabilities(now, backends)
    return backends


async def resolve_job_spec(
    db: AsyncSession,
    domain: str | None,
    outputs: frozenset[str],
    raw_options: str | None,
) -> JobSpec:
    """Validate a submit's domain, outputs and options (D7), in that order; 422 on the
    first that fails. A missing domain is ``default`` (D6)."""
    name = domain or DEFAULT_DOMAIN
    if not name.isprintable():
        # No domain has such a name, and Postgres would reject a NUL in the lookup (a 500).
        raise Unprocessable("domain must be printable text — see GET /api/v1/domains")
    try:
        options = parse_options(raw_options)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc

    backends = await _domain_capabilities(db, name)
    if not backends:
        raise Unprocessable(f"No backend serves domain '{name}' — see GET /api/v1/domains")

    # One backend has to produce all of them: a job isn't split across backends.
    if not any(outputs <= backend.outputs for backend in backends):
        offered = sorted({o for backend in backends for o in backend.outputs})
        raise Unprocessable(
            f"Domain '{name}' can't produce outputs {', '.join(sorted(outputs))} together; "
            f"its outputs are {', '.join(offered)}"
        )

    errors = options_errors(backends[0].options_schema, options)
    if errors:
        raise Unprocessable(f"Invalid options for domain '{name}': " + "; ".join(errors))

    adapters = {backend.adapter for backend in backends if outputs <= backend.outputs}
    reason = adapter_rejection(adapters, name, outputs, options)
    if reason is not None:
        raise Unprocessable(f"Invalid request for domain '{name}': {reason}")

    return JobSpec(domain=name, outputs=outputs, options=options)
