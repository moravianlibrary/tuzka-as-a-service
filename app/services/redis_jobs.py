"""Redis state machine for jobs: priority queues, in-flight tracking, and pub/sub events."""

import contextlib
import json
import time
from typing import Any, cast

import redis.asyncio as aioredis

from app.config import Settings
from app.engines.outputs import fmt_to_outputs
from app.services.load import ewma

settings = Settings()

# Priority queue key. Priority 0 is the default; higher numbers are drained first.
_PENDING_KEY = "jobs:pending:{priority}"
# Set of priority levels that have (or had) a pending queue. Every writer to a
# `jobs:pending:<n>` ZSET also records <n> here, so the submit worker can find the
# populated levels without a per-tick keyspace SCAN. It's a superset of the live
# levels (drained-empty levels linger harmlessly — a no-op zpopmin), never a subset,
# so no job is ever missed.
_PENDING_LEVELS_KEY = "jobs:pending:levels"

# Submit-worker wakeup list. signal_submit() pushes a token the submit worker blocks on
# (BLPOP) so newly-pending work or a freed slot dispatches immediately instead of waiting
# out the tick. The TTL keeps tokens from piling up if the worker is down.
_SUBMIT_WAKEUP_KEY = "jobs:submit_wakeup"
_SUBMIT_WAKEUP_TTL_SECONDS = 60

# Poller-worker wakeup list, mirroring the submit one. signal_poll() pushes a token the
# poller blocks on (BLPOP) so a job that just went in-flight (set_running) is polled at
# its scheduled first poll instead of waiting out an idle tick. The engine can't push
# completion, so completion is still discovered by polling — this only tightens the first
# poll after dispatch. The TTL keeps tokens from piling up if the poller is down.
_POLL_WAKEUP_KEY = "jobs:poll_wakeup"
_POLL_WAKEUP_TTL_SECONDS = 60

# Delayed-retry ZSET: job_id scored by ready-at unix time.
_DELAYED_KEY = "jobs:delayed"

# Load counters for GET /load: how many jobs are waiting (incl. delayed retries) and in
# flight, per "{domain}|{sorted,outputs}" field (load_field). Moved only by the state
# functions below, and only on a real transition, so re-running one is a no-op.
LOAD_QUEUED_KEY = "load:queued"
LOAD_INFLIGHT_KEY = "load:inflight"
# Seconds per page, per domain: an EWMA of engine run time, for /load's backlog_s.
LOAD_SPP_KEY = "load:spp"


async def signal_submit(r: aioredis.Redis) -> None:
    """Wake the submit worker — new pending work or a freed slot. One round-trip."""
    async with r.pipeline(transaction=False) as pipe:
        pipe.rpush(_SUBMIT_WAKEUP_KEY, b"1")
        pipe.expire(_SUBMIT_WAKEUP_KEY, _SUBMIT_WAKEUP_TTL_SECONDS)
        await pipe.execute()


async def wait_for_submit_signal(r: aioredis.Redis, timeout: float) -> None:
    """Block until a wakeup token arrives or ``timeout`` elapses, then drain the backlog
    so a burst of signals coalesces into a single dispatch pass."""
    await r.blpop(_SUBMIT_WAKEUP_KEY, timeout=timeout)
    await r.delete(_SUBMIT_WAKEUP_KEY)


async def signal_poll(r: aioredis.Redis) -> None:
    """Wake the poller — a job just entered the in-flight set. One round-trip."""
    async with r.pipeline(transaction=False) as pipe:
        pipe.rpush(_POLL_WAKEUP_KEY, b"1")
        pipe.expire(_POLL_WAKEUP_KEY, _POLL_WAKEUP_TTL_SECONDS)
        await pipe.execute()


async def wait_for_poll_signal(r: aioredis.Redis, timeout: float) -> None:
    """Block until a wakeup token arrives or ``timeout`` elapses, then drain the backlog
    so a burst of signals coalesces into a single poll pass. A non-positive timeout means
    'don't wait' — BLPOP treats 0 as block-forever, so return immediately instead."""
    if timeout <= 0:
        return
    await r.blpop(_POLL_WAKEUP_KEY, timeout=timeout)
    await r.delete(_POLL_WAKEUP_KEY)


def _pending_key(priority: int) -> str:
    return _PENDING_KEY.format(priority=priority)


def parse_avoid(meta: dict[str, str]) -> frozenset[int]:
    """Parse ``avoid_backends`` from job metadata into a frozenset of backend ids.

    The field is comma-separated numeric ids (deduplicated by the caller).
    Returns an empty frozenset for missing or empty values.
    """
    raw = meta.get("avoid_backends", "")
    if not raw:
        return frozenset()
    return frozenset(int(v) for v in raw.split(",") if v.strip())


def job_domain(meta: dict[str, str]) -> str:
    """The job's domain from its hash; a domain-less job is a ``default`` job."""
    return meta.get("domain") or "default"


def job_outputs(meta: dict[str, str]) -> frozenset[str]:
    """The job's requested outputs from its hash. A job enqueued before outputs were
    stored only has ``fmt``, which maps onto the same set."""
    raw = meta.get("outputs")
    if raw:
        return frozenset(o for o in raw.split(",") if o)
    return fmt_to_outputs(meta.get("fmt") or "multi")


def job_options(meta: dict[str, str]) -> dict[str, Any]:
    """The job's engine options from its hash (validated at submit); ``{}`` if none."""
    raw = meta.get("options")
    options: dict[str, Any] = json.loads(raw) if raw else {}
    return options


def load_field(domain: str, outputs: frozenset[str]) -> str:
    """The load-counter field for a (domain, outputs) pair, e.g. ``default|alto,txt``."""
    return f"{domain}|{','.join(sorted(outputs))}"


def parse_load_field(field: str) -> tuple[str, frozenset[str]]:
    """Inverse of ``load_field``."""
    domain, _, outputs = field.partition("|")
    return domain, frozenset(o for o in outputs.split(",") if o)


def _job_load_field(meta: dict[str, str]) -> str:
    return load_field(job_domain(meta), job_outputs(meta))


async def _add_pending(r: aioredis.Redis, priority: int, job_id: str, score: float) -> None:
    """Add a job to its priority ZSET, register the level, and wake the submit worker.

    Every path that makes a job dispatchable (enqueue, requeue, release-and-requeue)
    routes through here, so the wakeup is signalled in exactly one place."""
    await r.zadd(_pending_key(priority), {job_id: score})
    await r.sadd(_PENDING_LEVELS_KEY, str(priority))
    await signal_submit(r)


async def schedule_retry(
    r: aioredis.Redis,
    job_id: str,
    original_score: float,
    delay_s: float,
    avoid_backend_id: int | None,
) -> None:
    """Schedule a job for delayed retry.

    Stores ``queue_score`` on the job hash so ``promote_due_retries`` knows
    which priority ZSET to put it on.  Appends *avoid_backend_id* to the
    job's ``avoid_backends`` field (comma-separated, deduplicated).
    ZADDs to ``jobs:delayed`` with ``now() + delay_s``.  Does **not**
    signal submit — the delayed-retry timer fires later.
    """
    key = f"job:{job_id}"
    # Store the original score so promote can re-enqueue to the right level.
    await r.hset(key, "queue_score", str(original_score))
    # Accumulate avoid_backends (comma-separated, deduplicated).
    if avoid_backend_id is not None:
        existing = await r.hget(key, "avoid_backends") or ""
        existing_str = existing.decode() if isinstance(existing, bytes) else existing
        parts = existing_str.split(",") if existing_str else []
        parts = [v for v in parts if v.strip() and int(v.strip()) != avoid_backend_id]
        parts.append(str(avoid_backend_id))
        await r.hset(key, "avoid_backends", ",".join(parts))
    await r.zadd(_DELAYED_KEY, {job_id: time.time() + delay_s})


async def promote_due_retries(r: aioredis.Redis, now: float) -> int:
    """Promote delayed retries that are now due.

    ZRANGEBYSCORE ``jobs:delayed`` with score ``-inf now``. For each id,
    ZREM (only proceed if removed == 1 for idempotency with two workers),
    then ``_add_pending`` with the stored ``queue_score``.  Returns the
    number of jobs promoted.
    """
    members = cast("list[bytes | str]", await r.zrangebyscore(_DELAYED_KEY, "-inf", now))
    if not members:
        return 0

    count = 0
    for member in members:
        job_id = member.decode() if isinstance(member, bytes) else member
        removed = await r.zrem(_DELAYED_KEY, job_id)
        if removed != 1:
            continue  # another worker already promoted it

        meta = await get_job(r, job_id)
        if meta is None:
            continue
        priority = int(meta.get("priority", 0))
        queue_score = float(meta.get("queue_score", time.time()))
        await _add_pending(r, priority, job_id, queue_score)
        count += 1
    return count


async def enqueue_job(
    r: aioredis.Redis, job_id: str, metadata: dict[str, str], state_ttl: int
) -> None:
    priority = int(metadata.get("priority", 0))
    key = f"job:{job_id}"
    # redis-py's `mapping` param uses an invariant key type, so a concrete dict[str, str]
    # does not upcast; cast around the third-party stub friction (values are all strings).
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(key, mapping=cast("dict[Any, Any]", metadata))
        pipe.expire(key, state_ttl)
        pipe.hincrby(LOAD_QUEUED_KEY, _job_load_field(metadata), 1)
        await pipe.execute()
    await _add_pending(r, priority, job_id, float(metadata.get("submitted_at", time.time())))


async def dequeue_jobs(r: aioredis.Redis, count: int) -> list[tuple[str, float]]:
    """Drain up to ``count`` jobs from priority queues, highest priority first.

    Reads the registered priority levels (``jobs:pending:levels``) and drains their
    queues in descending priority order.
    """
    members = await r.smembers(_PENDING_LEVELS_KEY)
    prios: list[int] = []
    for m in members:
        with contextlib.suppress(ValueError):
            prios.append(int(m.decode() if isinstance(m, bytes) else m))
    prios.sort(reverse=True)
    if not prios:
        prios = [0]

    results: list[tuple[str, float]] = []
    remaining = count

    for prio in prios:
        if remaining <= 0:
            break
        # zpopmin returns [(member, score), ...]; the stub over-broadens this to a union,
        # so narrow to the documented shape for the decode step.
        batch = cast(
            "list[tuple[bytes | str, float]]", await r.zpopmin(_pending_key(prio), remaining)
        )
        for member, score in batch:
            member_str = member.decode() if isinstance(member, bytes) else member
            results.append((member_str, float(score)))
        remaining -= len(batch)

    return results


async def get_job(r: aioredis.Redis, job_id: str) -> dict[str, str] | None:
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
    # Only a job that really entered the in-flight set moves from queued to inflight,
    # so a repeated call can't skew the load counters.
    if await r.sadd("jobs:inflight", job_id):
        meta = await get_job(r, job_id)
        async with r.pipeline(transaction=True) as pipe:
            pipe.incr(f"backend:{backend_id}:inflight")
            if meta is not None:
                field = _job_load_field(meta)
                pipe.hincrby(LOAD_QUEUED_KEY, field, -1)
                pipe.hincrby(LOAD_INFLIGHT_KEY, field, 1)
            await pipe.execute()
    await signal_poll(r)  # a job just went in-flight — start polling it promptly


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
    if removed and meta is not None:
        await r.hincrby(LOAD_INFLIGHT_KEY, _job_load_field(meta), -1)
    if backend_id and removed:
        await r.decr(f"backend:{backend_id}:inflight")
        await signal_submit(r)  # a slot just freed — let the worker refill it now


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
    if meta is not None:
        # A job that never ran (failed at dispatch, timed out in the queue) leaves the
        # queued count; one that ran leaves inflight. Already failed/done: nothing.
        if removed:
            await r.hincrby(LOAD_INFLIGHT_KEY, _job_load_field(meta), -1)
        elif meta.get("status") == "queued":
            await r.hincrby(LOAD_QUEUED_KEY, _job_load_field(meta), -1)
    if backend_id and removed:
        await r.decr(f"backend:{backend_id}:inflight")
        await signal_submit(r)  # a slot just freed — let the worker refill it now


async def requeue_job(r: aioredis.Redis, job_id: str, original_score: float) -> None:
    meta = await get_job(r, job_id)
    priority = int(meta.get("priority", 0)) if meta else 0
    await _add_pending(r, priority, job_id, original_score)


async def release_and_requeue(
    r: aioredis.Redis,
    job_id: str,
    score: float,
    state_ttl: int,
    delay_s: float = 0.0,
    avoid_backend_id: int | None = None,
) -> None:
    """Release an in-flight job's slot and put it back on the pending queue.

    For jobs whose engine pod became unreachable: drop it from the inflight set,
    release the (idempotent) backend counter, reset the hash to queued, and re-add to
    the appropriate priority queue so the submit worker dispatches it to a healthy backend.

    If ``delay_s > 0`` the job is scheduled for delayed retry (with optional
    ``avoid_backend_id``) instead of being re-added immediately.
    """
    key = f"job:{job_id}"
    meta = await get_job(r, job_id)
    backend_id = meta.get("backend_id") if meta else None
    removed = await r.srem("jobs:inflight", job_id)
    if removed and meta is not None:
        field = _job_load_field(meta)
        async with r.pipeline(transaction=True) as pipe:
            pipe.hincrby(LOAD_INFLIGHT_KEY, field, -1)
            pipe.hincrby(LOAD_QUEUED_KEY, field, 1)
            await pipe.execute()
    if backend_id and removed:
        await r.decr(f"backend:{backend_id}:inflight")
    await r.hset(key, mapping={"status": "queued"})
    await r.hdel(key, "engine_job_id", "backend_url", "backend_id")
    await r.expire(key, state_ttl)

    if delay_s > 0:
        await schedule_retry(r, job_id, score, delay_s, avoid_backend_id)
    else:
        priority = int(meta.get("priority", 0)) if meta else 0
        await _add_pending(r, priority, job_id, score)


async def record_page_seconds(r: aioredis.Redis, domain: str, seconds: float) -> None:
    """Fold one finished page's engine run time into the domain's seconds-per-page
    average. Two pollers racing can drop a sample; for an average that's harmless."""
    prev = await r.hget(LOAD_SPP_KEY, domain)
    value = ewma(float(prev) if prev is not None else None, seconds)
    await r.hset(LOAD_SPP_KEY, domain, str(value))


async def get_inflight_ids(r: aioredis.Redis) -> set[str]:
    members = await r.smembers("jobs:inflight")
    return {m.decode() if isinstance(m, bytes) else m for m in members}


async def get_backend_inflight(r: aioredis.Redis, backend_id: int) -> int:
    val = await r.get(f"backend:{backend_id}:inflight")
    return int(val) if val else 0


async def publish_event(r: aioredis.Redis, username: str, event: dict[str, str]) -> None:
    channel = f"job:{username}:events"
    await r.publish(channel, json.dumps(event))
