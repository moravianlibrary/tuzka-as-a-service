"""Domain sync: turn an engine's ``describe()`` into ``domains`` + ``backend_domains`` rows.

The submit worker runs this per backend on healthcheck. It is the only writer of
``backend_domains``. ``plan_domain_links`` holds the rules and is pure; the two async
functions are the thin DB side.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines.base import EngineInfo
from app.models.backend import Backend
from app.models.backend_domain import BackendDomain
from app.models.domain import Domain
from app.vocabulary import DomainSource

logger = logging.getLogger(__name__)


def plan_domain_links(
    info: EngineInfo,
    known_schemas: dict[str, dict[str, Any] | None],
) -> tuple[dict[str, frozenset[str]], dict[str, dict[str, Any]], list[str]]:
    """Decide what a backend's describe() means for the DB.

    Returns ``(links, schemas_to_store, changed)``:

    * ``links`` — domain → the outputs this backend produces for it.
    * ``schemas_to_store`` — domains whose options schema should be written: new ones,
      and ones whose stored schema differs (newest wins, so engine rolling upgrades
      keep working). A backend that describes no schema never clears a stored one.
    * ``changed`` — the subset of ``schemas_to_store`` that replaces a different stored
      schema, for the caller to warn about.
    """
    links = {name: domain.outputs for name, domain in info.domains.items()}
    schemas_to_store: dict[str, dict[str, Any]] = {}
    changed: list[str] = []
    for name, domain in sorted(info.domains.items()):
        schema = domain.options_schema
        if schema is None:
            continue
        known = known_schemas.get(name)
        if known == schema:
            continue
        schemas_to_store[name] = schema
        if known is not None:
            changed.append(name)
    return links, schemas_to_store, changed


async def _domain_rows(
    db: AsyncSession, names: list[str]
) -> list[tuple[int, str, dict[str, Any] | None]]:
    """Make sure every name has a ``domains`` row; return ``(id, name, options_schema)``."""
    if names:
        await db.execute(
            insert(Domain)
            .values([{"name": n} for n in names])
            .on_conflict_do_nothing(index_elements=["name"])
        )
    rows = await db.execute(
        select(Domain.id, Domain.name, Domain.options_schema).where(Domain.name.in_(names))
    )
    return [(domain_id, name, schema) for domain_id, name, schema in rows.all()]


async def apply_domain_sync(db: AsyncSession, backend_id: int, info: EngineInfo) -> None:
    """Upsert the described domains, store their schemas and rebuild this backend's
    ``backend_domains`` rows (engine rows replace declared ones). The caller commits.

    An empty describe() changes nothing: an engine that is down or scaled to zero keeps
    its remembered capabilities, which is what lets /load ask for it to come back (D7).
    """
    names = sorted(info.domains)
    if not names:
        return
    ids: dict[str, int] = {}
    known_schemas: dict[str, dict[str, Any] | None] = {}
    for domain_id, name, schema in await _domain_rows(db, names):
        ids[name] = domain_id
        known_schemas[name] = schema

    links, schemas_to_store, changed = plan_domain_links(info, known_schemas)
    for name in changed:
        logger.warning(
            "Options schema of domain %r changed (backend %d); storing the new one",
            name,
            backend_id,
        )
    for name, schema in schemas_to_store.items():
        await db.execute(update(Domain).where(Domain.id == ids[name]).values(options_schema=schema))

    await db.execute(delete(BackendDomain).where(BackendDomain.backend_id == backend_id))
    if links:
        await db.execute(
            insert(BackendDomain).values(
                [
                    {"backend_id": backend_id, "domain_id": ids[name], "outputs": sorted(outputs)}
                    for name, outputs in links.items()
                ]
            )
        )


async def apply_declared_capabilities(
    db: AsyncSession, backend_id: int, declared: dict[str, list[str]]
) -> None:
    """Record capabilities declared at registration (``source='declared'``), for domains
    the engine hasn't reported itself yet; engine rows are never overwritten. The caller
    commits."""
    names = sorted(declared)
    for domain_id, name, _ in await _domain_rows(db, names):
        outputs = sorted(set(declared[name]))
        stmt = insert(BackendDomain).values(
            backend_id=backend_id,
            domain_id=domain_id,
            outputs=outputs,
            source=DomainSource.DECLARED,
        )
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=["backend_id", "domain_id"],
                set_={"outputs": stmt.excluded.outputs},
                where=BackendDomain.source == DomainSource.DECLARED,
            )
        )


async def served_capabilities(db: AsyncSession, backend_id: int) -> dict[str, frozenset[str]]:
    """Domain → outputs that ``backend_id`` currently serves (from the synced mapping)."""
    rows = await db.execute(
        select(Domain.name, BackendDomain.outputs)
        .join(BackendDomain, BackendDomain.domain_id == Domain.id)
        .where(BackendDomain.backend_id == backend_id)
    )
    return {name: frozenset(outputs) for name, outputs in rows.all()}


async def all_served_capabilities(db: AsyncSession) -> dict[int, dict[str, frozenset[str]]]:
    """Backend id → domain → outputs for every backend, in one query (the submit worker
    reads this once per refresh instead of once per backend per tick)."""
    rows = await db.execute(
        select(BackendDomain.backend_id, Domain.name, BackendDomain.outputs).join(
            Domain, Domain.id == BackendDomain.domain_id
        )
    )
    caps: dict[int, dict[str, frozenset[str]]] = {}
    for backend_id, name, outputs in rows.all():
        caps.setdefault(backend_id, {})[name] = frozenset(outputs)
    return caps


async def adapter_capabilities(
    db: AsyncSession, adapter: str | None = None
) -> dict[str, dict[str, frozenset[str]]]:
    """Adapter → domain → outputs, the union over all its backends' synced rows.
    Disabled backends count too: a workload scaled to zero is still registered, and its
    remembered capabilities are what lets /load ask for it to scale back up."""
    query = (
        select(Backend.adapter, Domain.name, BackendDomain.outputs)
        .join(BackendDomain, BackendDomain.backend_id == Backend.id)
        .join(Domain, Domain.id == BackendDomain.domain_id)
    )
    if adapter is not None:
        query = query.where(Backend.adapter == adapter)
    caps: dict[str, dict[str, frozenset[str]]] = {}
    for name, domain, outputs in (await db.execute(query)).all():
        served = caps.setdefault(name, {})
        served[domain] = served.get(domain, frozenset()) | frozenset(outputs)
    return caps
