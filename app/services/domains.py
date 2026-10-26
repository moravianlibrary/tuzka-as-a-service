"""The domains taas serves right now: those at least one enabled backend serves, with
what they produce. Read for ``GET /domains``."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession

# One row per domain served by at least one enabled backend; outputs are unnested so
# a domain served by several backends reports the union of what they produce.
_SERVED_DOMAINS_SQL = text(
    """
    SELECT d.name, d.label, d.description, d.options_schema, d.legacy_engine_id,
           array_agg(DISTINCT o.output ORDER BY o.output) AS outputs
    FROM domains d
    JOIN backend_domains bd ON bd.domain_id = d.id
    JOIN backends b ON b.id = bd.backend_id AND b.enabled
    CROSS JOIN LATERAL unnest(bd.outputs) AS o(output)
    GROUP BY d.id
    ORDER BY d.name
    """
).columns(
    name=Text,
    label=Text,
    description=Text,
    options_schema=JSONB,
    legacy_engine_id=Integer,
    outputs=ARRAY(Text),
)


@dataclass(frozen=True)
class ServedDomain:
    """A domain at least one enabled backend serves, and every output they produce."""

    name: str
    label: str | None
    description: str | None
    options_schema: dict[str, Any] | None
    legacy_engine_id: int | None
    outputs: list[str]


async def served_domains(db: AsyncSession) -> list[ServedDomain]:
    """Every served domain, by name. Raw SQL: the lateral unnest of each backend's
    outputs is what gives a domain served by several backends their union."""
    rows = (await db.execute(_SERVED_DOMAINS_SQL)).mappings()
    return [ServedDomain(**row) for row in rows]
