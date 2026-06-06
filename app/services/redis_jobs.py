import json
import time

import redis.asyncio as aioredis

from app.config import Settings

settings = Settings()


async def enqueue_job(r: aioredis.Redis, job_id: str, metadata: dict) -> None:
    key = f"job:{job_id}"
    await r.hset(key, mapping=metadata)
    await r.expire(key, settings.job_ttl_seconds)
    await r.zadd("jobs:pending", {job_id: metadata.get("submitted_at", time.time())})


async def dequeue_jobs(r: aioredis.Redis, count: int) -> list[tuple[str, float]]:
    results = await r.zpopmin("jobs:pending", count)
    return [
        (member.decode() if isinstance(member, bytes) else member, score)
        for member, score in results
    ]


async def get_job(r: aioredis.Redis, job_id: str) -> dict | None:
    data = await r.hgetall(f"job:{job_id}")
    if not data:
        return None
    return {
        k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
        for k, v in data.items()
    }


async def set_running(
    r: aioredis.Redis,
    job_id: str,
    engine_job_id: str,
    backend_url: str,
    backend_id: int,
) -> None:
    key = f"job:{job_id}"
    await r.hset(
        key,
        mapping={
            "status": "running",
            "engine_job_id": engine_job_id,
            "backend_url": backend_url,
            "backend_id": str(backend_id),
            "next_poll_at": str(time.time() + settings.poll_backoff_initial),
        },
    )
    await r.expire(key, settings.job_ttl_seconds)
    await r.sadd("jobs:inflight", job_id)
    await r.incr(f"backend:{backend_id}:inflight")


async def set_done(r: aioredis.Redis, job_id: str) -> None:
    key = f"job:{job_id}"
    meta = await get_job(r, job_id)
    backend_id = meta.get("backend_id") if meta else None
    await r.hset(
        key,
        mapping={"status": "done", "finished_at": str(time.time())},
    )
    await r.expire(key, settings.job_ttl_seconds)
    await r.srem("jobs:inflight", job_id)
    if backend_id:
        await r.decr(f"backend:{backend_id}:inflight")


async def set_failed(r: aioredis.Redis, job_id: str, error: str) -> None:
    key = f"job:{job_id}"
    meta = await get_job(r, job_id)
    backend_id = meta.get("backend_id") if meta else None
    await r.hset(
        key,
        mapping={"status": "failed", "error": error, "finished_at": str(time.time())},
    )
    await r.expire(key, settings.job_ttl_seconds)
    await r.srem("jobs:inflight", job_id)
    if backend_id:
        await r.decr(f"backend:{backend_id}:inflight")


async def requeue_job(r: aioredis.Redis, job_id: str, original_score: float) -> None:
    await r.zadd("jobs:pending", {job_id: original_score})


async def get_inflight_ids(r: aioredis.Redis) -> set[str]:
    members = await r.smembers("jobs:inflight")
    return {m.decode() if isinstance(m, bytes) else m for m in members}


async def get_backend_inflight(r: aioredis.Redis, backend_id: int) -> int:
    val = await r.get(f"backend:{backend_id}:inflight")
    return int(val) if val else 0


async def publish_event(r: aioredis.Redis, username: str, event: dict) -> None:
    channel = f"job:{username}:events"
    await r.publish(channel, json.dumps(event))
