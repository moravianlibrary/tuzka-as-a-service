import json
import time

import redis.asyncio as aioredis

from app.config import Settings

settings = Settings()

# Priority queue key. Priority 0 is the default; higher numbers are drained first.
_PENDING_KEY = "jobs:pending:{priority}"
# Set of priority levels that have (or had) a pending queue. Every writer to a
# `jobs:pending:<n>` ZSET also records <n> here, so the submit worker can find the
# populated levels without a per-tick keyspace SCAN. It's a superset of the live
# levels (drained-empty levels linger harmlessly — a no-op zpopmin), never a subset,
# so no job is ever missed.
_PENDING_LEVELS_KEY = "jobs:pending:levels"


def _pending_key(priority: int) -> str:
    return _PENDING_KEY.format(priority=priority)


async def _add_pending(r: aioredis.Redis, priority: int, job_id: str, score: float) -> None:
    """Add a job to its priority ZSET and register the level for discovery."""
    await r.zadd(_pending_key(priority), {job_id: score})
    await r.sadd(_PENDING_LEVELS_KEY, str(priority))


async def enqueue_job(r: aioredis.Redis, job_id: str, metadata: dict, state_ttl: int) -> None:
    priority = int(metadata.get("priority", 0))
    key = f"job:{job_id}"
    await r.hset(key, mapping=metadata)
    await r.expire(key, state_ttl)
    await _add_pending(r, priority, job_id, metadata.get("submitted_at", time.time()))


async def dequeue_jobs(r: aioredis.Redis, count: int) -> list[tuple[str, float]]:
    """Drain up to ``count`` jobs from priority queues, highest priority first.

    Reads the registered priority levels (``jobs:pending:levels``) and drains their
    queues in descending priority order.
    """
    members = await r.smembers(_PENDING_LEVELS_KEY)
    prios: list[int] = []
    for m in members:
        try:
            prios.append(int(m.decode() if isinstance(m, bytes) else m))
        except ValueError:
            pass
    prios.sort(reverse=True)
    if not prios:
        prios = [0]

    results: list[tuple[str, float]] = []
    remaining = count

    for prio in prios:
        if remaining <= 0:
            break
        batch = await r.zpopmin(_pending_key(prio), remaining)
        for member, score in batch:
            results.append(
                (member.decode() if isinstance(member, bytes) else member, score)
            )
        remaining -= len(batch)

    return results


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
    state_ttl: int,
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
    await r.expire(key, state_ttl)
    await r.sadd("jobs:inflight", job_id)
    await r.incr(f"backend:{backend_id}:inflight")


async def set_done(r: aioredis.Redis, job_id: str, state_ttl: int) -> None:
    key = f"job:{job_id}"
    meta = await get_job(r, job_id)
    backend_id = meta.get("backend_id") if meta else None
    await r.hset(
        key,
        mapping={"status": "done", "finished_at": str(time.time())},
    )
    await r.expire(key, state_ttl)
    # Only decrement when srem actually removed the job: makes slot-release
    # idempotent, so a re-run (e.g. reaper re-processing after a failed commit)
    # can't double-decrement the backend counter.
    removed = await r.srem("jobs:inflight", job_id)
    if backend_id and removed:
        await r.decr(f"backend:{backend_id}:inflight")


async def set_failed(r: aioredis.Redis, job_id: str, error: str, state_ttl: int) -> None:
    key = f"job:{job_id}"
    meta = await get_job(r, job_id)
    backend_id = meta.get("backend_id") if meta else None
    await r.hset(
        key,
        mapping={"status": "failed", "error": error, "finished_at": str(time.time())},
    )
    await r.expire(key, state_ttl)
    # Only decrement when srem actually removed the job: makes slot-release
    # idempotent, so a re-run (e.g. reaper re-processing after a failed commit)
    # can't double-decrement the backend counter.
    removed = await r.srem("jobs:inflight", job_id)
    if backend_id and removed:
        await r.decr(f"backend:{backend_id}:inflight")


async def requeue_job(r: aioredis.Redis, job_id: str, original_score: float) -> None:
    meta = await get_job(r, job_id)
    priority = int(meta.get("priority", 0)) if meta else 0
    await _add_pending(r, priority, job_id, original_score)


async def release_and_requeue(r: aioredis.Redis, job_id: str, score: float, state_ttl: int) -> None:
    """Release an in-flight job's slot and put it back on the pending queue.

    For jobs whose engine pod became unreachable: drop it from the inflight set,
    release the (idempotent) backend counter, reset the hash to queued, and re-add to
    the appropriate priority queue so the submit worker dispatches it to a healthy backend.
    """
    key = f"job:{job_id}"
    meta = await get_job(r, job_id)
    backend_id = meta.get("backend_id") if meta else None
    priority = int(meta.get("priority", 0)) if meta else 0
    removed = await r.srem("jobs:inflight", job_id)
    if backend_id and removed:
        await r.decr(f"backend:{backend_id}:inflight")
    await r.hset(key, mapping={"status": "queued"})
    await r.hdel(key, "engine_job_id", "backend_url", "backend_id")
    await r.expire(key, state_ttl)
    await _add_pending(r, priority, job_id, score)


async def get_inflight_ids(r: aioredis.Redis) -> set[str]:
    members = await r.smembers("jobs:inflight")
    return {m.decode() if isinstance(m, bytes) else m for m in members}


async def get_backend_inflight(r: aioredis.Redis, backend_id: int) -> int:
    val = await r.get(f"backend:{backend_id}:inflight")
    return int(val) if val else 0


async def publish_event(r: aioredis.Redis, username: str, event: dict) -> None:
    channel = f"job:{username}:events"
    await r.publish(channel, json.dumps(event))
