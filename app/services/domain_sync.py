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
from app.models.backend_domain import BackendDomain
from app.models.domain import Domain

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


async def apply_domain_sync(db: AsyncSession, backend_id: int, info: EngineInfo) -> None:
    """Upsert the described domains, store their schemas and rebuild this backend's
    ``backend_domains`` rows. The caller commits."""
    names = sorted(info.domains)
    if names:
        await db.execute(
            insert(Domain)
            .values([{"name": n} for n in names])
            .on_conflict_do_nothing(index_elements=["name"])
        )
    rows = await db.execute(
        select(Domain.id, Domain.name, Domain.options_schema).where(Domain.name.in_(names))
    )
    ids: dict[str, int] = {}
    known_schemas: dict[str, dict[str, Any] | None] = {}
    for domain_id, name, schema in rows.all():
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


async def served_capabilities(db: AsyncSession, backend_id: int) -> dict[str, frozenset[str]]:
    """Domain → outputs that ``backend_id`` currently serves (from the synced mapping)."""
    rows = await db.execute(
        select(Domain.name, BackendDomain.outputs)
        .join(BackendDomain, BackendDomain.domain_id == Domain.id)
        .where(BackendDomain.backend_id == backend_id)
    )
    return {name: frozenset(outputs) for name, outputs in rows.all()}
