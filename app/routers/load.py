"""``GET /load``: queued work in seconds, for an autoscaler (KEDA ``metrics-api``)."""

from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_redis, require_load_token
from app.models.db import get_db
from app.services import config as config_service
from app.services.domain_sync import adapter_capabilities
from app.services.load import compute_load, group_load
from app.services.redis_jobs import LOAD_INFLIGHT_KEY, LOAD_QUEUED_KEY, LOAD_SPP_KEY

router = APIRouter()


def _decode(raw: dict[Any, Any]) -> dict[str, str]:
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }


@router.get("/load", dependencies=[Depends(require_load_token)])
async def get_load(
    domain: str | None = None,
    adapter: str | None = None,
    group_by: Literal["domain", "adapter"] | None = None,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """How much work is waiting, as ``{queued, inflight, backlog_s}``.

    ``domain=a,b`` counts only those domains. ``adapter=x`` counts only jobs a backend
    of adapter ``x`` could serve (its domains, with all the requested outputs), using
    remembered capabilities, so a workload scaled to zero still sees its demand.
    ``group_by=domain|adapter`` returns ``{"groups": {name: totals}}`` instead; by
    adapter, a job several adapters can serve counts in each of their groups.
    """
    async with r.pipeline(transaction=False) as pipe:
        pipe.hgetall(LOAD_QUEUED_KEY)
        pipe.hgetall(LOAD_INFLIGHT_KEY)
        pipe.hgetall(LOAD_SPP_KEY)
        raw_queued, raw_inflight, raw_spp = await pipe.execute()
    queued = {k: int(v) for k, v in _decode(raw_queued).items()}
    inflight = {k: int(v) for k, v in _decode(raw_inflight).items()}
    spp = {k: float(v) for k, v in _decode(raw_spp).items()}
    default_spp = await config_service.get_default_seconds_per_page(db)
    domains = {d.strip() for d in domain.split(",") if d.strip()} if domain else None

    if group_by is not None:
        caps_by_adapter = await adapter_capabilities(db, adapter) if group_by == "adapter" else {}
        groups = group_load(queued, inflight, spp, default_spp, group_by, caps_by_adapter, domains)
        return {"groups": {name: totals.as_dict() for name, totals in groups.items()}}

    caps = None
    if adapter is not None:
        caps = (await adapter_capabilities(db, adapter)).get(adapter, {})
    return compute_load(queued, inflight, spp, default_spp, domains, caps).as_dict()
