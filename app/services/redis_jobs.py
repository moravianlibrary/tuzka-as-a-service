"""Redis state machine for jobs: priority queues, in-flight tracking, and pub/sub events."""

import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any, cast

import redis.asyncio as aioredis

from app.config import Settings
from app.engines.outputs import fmt_to_outputs
from app.services.load import ewma, load_field

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

# Durable harvest queue: job_id scored by its next harvest attempt (unix time). A job
# whose engine finished but whose results are not stored yet lives here instead of
# jobs:inflight. The engine slot goes back to the dispatcher as soon as the engine is
# done, so a slow harvest (fetch, compress, store, DB) does not idle the backend; the
# poller drains this queue at its own pace. The entry and the engine's last status are
# written in the same atomic step as the slot release, so a poller crash between
# engine-done and harvest loses no work.
PENDING_HARVEST_KEY = "jobs:pending_harvest"

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
    # The status lets set_failed tell a job that never ran (queued count) from one that
    # did (inflight count).
    fields = {"status": "queued", **metadata}
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(key, mapping=cast("dict[Any, Any]", fields))
        pipe.expire(key, state_ttl)
        pipe.hincrby(LOAD_QUEUED_KEY, _job_load_field(metadata), 1)
        await pipe.execute()
    await _add_pending(r, priority, job_id, float(metadata.get("submitted_at", time.time())))


async def _pending_levels(r: aioredis.Redis) -> list[int]:
    """The registered priority levels, highest first (``[0]`` when none are)."""
    members = await r.smembers(_PENDING_LEVELS_KEY)
    prios: list[int] = []
    for m in members:
        with contextlib.suppress(ValueError):
            prios.append(int(m.decode() if isinstance(m, bytes) else m))
    prios.sort(reverse=True)
    return prios or [0]


async def peek_pending(r: aioredis.Redis, count: int) -> list[tuple[str, float, int]]:
    """The first ``count`` pending jobs as ``(job_id, score, priority)``, highest priority
    first and oldest first within one. Nothing is removed: a job leaves its queue only when
    a worker claims it (``claim_pending``), so one that no backend can serve yet, or that
    a worker was holding when it died, is never lost."""
    results: list[tuple[str, float, int]] = []
    for prio in await _pending_levels(r):
        remaining = count - len(results)
        if remaining <= 0:
            break
        # zrange withscores returns [(member, score), ...]; the stub over-broadens it.
        batch = cast(
            "list[tuple[bytes | str, float]]",
            await r.zrange(_pending_key(prio), 0, remaining - 1, withscores=True),
        )
        for member, score in batch:
            member_str = member.decode() if isinstance(member, bytes) else member
            results.append((member_str, float(score), prio))
    return results


async def claim_pending(r: aioredis.Redis, job_id: str, priority: int) -> bool:
    """Take a peeked job off its queue to dispatch it. True for exactly one worker; False
    when another one claimed it first. ``claimed_at`` tells ``requeue_orphans`` a job
    being dispatched right now from one that was lost."""
    async with r.pipeline(transaction=True) as pipe:
        pipe.zrem(_pending_key(priority), job_id)
        pipe.hset(f"job:{job_id}", "claimed_at", str(time.time()))
        removed, _ = await pipe.execute()
    return bool(removed == 1)


# requeue_orphans' check-and-requeue for one job, atomic so a submit worker can't claim
# the job between the check and the ZADD (that dispatched it twice).
# KEYS: job hash, delayed ZSET, levels SET. ARGV: job id, now, claim grace, pending prefix.
_REQUEUE_ORPHAN_LUA = """
local f = redis.call('HMGET', KEYS[1], 'status', 'priority', 'submitted_at', 'claimed_at')
if f[1] ~= 'queued' then return 0 end
if redis.call('ZSCORE', KEYS[2], ARGV[1]) then return 0 end
local now = tonumber(ARGV[2])
if f[4] and now - tonumber(f[4]) < tonumber(ARGV[3]) then return 0 end
local prio = f[2] or '0'
local pending = ARGV[4] .. prio
if redis.call('ZSCORE', pending, ARGV[1]) then return 0 end
redis.call('ZADD', pending, f[3] and tonumber(f[3]) or now, ARGV[1])
redis.call('SADD', KEYS[3], prio)
return 1
"""


async def requeue_orphans(
    r: aioredis.Redis, job_ids: list[str], now: float, claim_grace_s: float = 300.0
) -> list[str]:
    """Put back on their queue the jobs among ``job_ids`` (queued in Postgres) that Redis
    still has as queued but in no queue: claimed by a worker that died before dispatching
    them. A job claimed in the last ``claim_grace_s`` is left alone, since its dispatch may
    still be running. Each job is checked and requeued in one script, so a claim can't
    slip in between. Returns the ids requeued."""
    if not job_ids:
        return []
    sha = await r.script_load(_REQUEUE_ORPHAN_LUA)
    prefix = _PENDING_KEY.format(priority="")
    async with r.pipeline(transaction=False) as pipe:
        for job_id in job_ids:
            keys = (f"job:{job_id}", _DELAYED_KEY, _PENDING_LEVELS_KEY)
            pipe.evalsha(sha, len(keys), *keys, job_id, now, claim_grace_s, prefix)
        replies = await pipe.execute()
    requeued = [job_id for job_id, added in zip(job_ids, replies, strict=True) if added]
    if requeued:
        await signal_submit(r)
    return requeued


async def get_job(r: aioredis.Redis, job_id: str) -> dict[str, str] | None:
    data = await r.hgetall(f"job:{job_id}")
    if not data:
        return None
    return {
        k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
        for k, v in data.items()
    }


async def get_jobs(r: aioredis.Redis, job_ids: list[str]) -> dict[str, dict[str, str] | None]:
    """``get_job`` for many jobs in one round trip (a pipeline of HGETALLs)."""
    async with r.pipeline(transaction=False) as pipe:
        for job_id in job_ids:
            pipe.hgetall(f"job:{job_id}")
        raw = await pipe.execute()
    return {
        job_id: {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in data.items()
        }
        if data
        else None
        for job_id, data in zip(job_ids, raw, strict=True)
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
                if meta.get("batch_id"):
                    # Which batches this backend is working on, for dispatch affinity
                    # (step 8, D7).
                    pipe.hincrby(_backend_batches_key(backend_id), _s(meta["batch_id"]), 1)
            await pipe.execute()
    await signal_poll(r)  # a job just went in-flight — start polling it promptly


@dataclass(frozen=True)
class BatchCounts:
    """A batch's progress, from its Redis counters."""

    batch_id: str
    total: int
    done: int
    failed: int


def _batch_key(batch_id: str) -> str:
    return f"batch:{batch_id}"


async def init_batch(r: aioredis.Redis, batch_id: str, total: int, ttl: int) -> None:
    """Start a batch's progress counters (D8); the state functions move them."""
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(_batch_key(batch_id), mapping={"total": total, "done": 0, "failed": 0})
        pipe.expire(_batch_key(batch_id), ttl)
        await pipe.execute()


async def _count_batch_page(
    r: aioredis.Redis, meta: dict[str, str] | None, field: str
) -> BatchCounts | None:
    """Count one finished page of a batch, unless the job had already finished (a
    re-run of a state function mustn't count it twice). None if it isn't a batch page."""
    if not meta or not meta.get("batch_id") or meta.get("status") in ("done", "failed"):
        return None
    key = _batch_key(meta["batch_id"])
    async with r.pipeline(transaction=True) as pipe:
        pipe.hincrby(key, field, 1)
        pipe.hgetall(key)
        _, raw = await pipe.execute()
    counts = {_s(k): int(v) for k, v in raw.items()}
    return BatchCounts(
        meta["batch_id"], counts.get("total", 0), counts.get("done", 0), counts.get("failed", 0)
    )


def batch_event(counts: BatchCounts) -> dict[str, Any]:
    """The WebSocket progress event of a batch (D8)."""
    return {
        "type": "batch",
        "batch_id": counts.batch_id,
        "total": counts.total,
        "done": counts.done,
        "failed": counts.failed,
    }


def _s(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


# Move a finished job from the in-flight set to the durable harvest queue, in one atomic
# step. The SREM result gates the whole transition, so two pollers that both see the
# engine done can't release the slot (or enqueue the harvest) twice. The engine's last
# status goes into the job hash, so the harvest has its timestamps even after a crash.
# KEYS: inflight set, harvest queue, job hash, load:inflight, backend inflight, batches
# ARGV: job id, now, load field, backend id, batch id, engine status JSON
_RELEASE_DONE_LUA = """
if redis.call('SREM', KEYS[1], ARGV[1]) == 0 then
  return 0
end
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
if redis.call('EXISTS', KEYS[3]) == 1 then
  redis.call('HSET', KEYS[3], 'harvest_times', ARGV[6], 'harvest_attempts', 0)
end
redis.call('HINCRBY', KEYS[4], ARGV[3], -1)
if ARGV[4] ~= '' then
  redis.call('DECR', KEYS[5])
  if ARGV[5] ~= '' then
    local left = redis.call('HINCRBY', KEYS[6], ARGV[5], -1)
    if left <= 0 then redis.call('HDEL', KEYS[6], ARGV[5]) end
  end
end
return 1
"""


async def release_done(
    r: aioredis.Redis,
    job_id: str,
    meta: dict[str, str],
    times: dict[str, Any] | None = None,
    now: float | None = None,
) -> bool:
    """Release a finished job's engine slot and queue its harvest, atomically.

    This is the engine-done transition: the slot must be reusable by the next job now,
    not after the harvest, which fetches outputs, stores objects and writes the result
    rows (seconds at load). The job waits in ``PENDING_HARVEST_KEY`` until ``set_done``
    or ``set_failed`` removes it. Returns True when this call made the transition; a
    duplicate done-poll returns False and changes nothing."""
    backend_id = meta.get("backend_id") or ""
    batch_id = meta.get("batch_id") or ""
    sha = await r.script_load(_RELEASE_DONE_LUA)
    released = await r.evalsha(
        sha,
        6,
        "jobs:inflight",
        PENDING_HARVEST_KEY,
        f"job:{job_id}",
        LOAD_INFLIGHT_KEY,
        f"backend:{backend_id}:inflight" if backend_id else "backend:0:inflight",
        _backend_batches_key(backend_id) if backend_id else "backend:0:batches",
        job_id,
        now if now is not None else time.time(),
        _job_load_field(meta),
        backend_id,
        batch_id,
        json.dumps(times or {}, default=str),
    )
    if released:
        await signal_submit(r)  # the engine slot is free — refill it now
    return bool(released)


async def due_harvest_ids(r: aioredis.Redis, limit: int, now: float | None = None) -> list[str]:
    """Jobs whose next harvest attempt is due, soonest first."""
    at = now if now is not None else time.time()
    members = cast(
        "list[bytes]",
        await r.zrangebyscore(PENDING_HARVEST_KEY, "-inf", at, start=0, num=limit),
    )
    return [_s(m) for m in members]


async def next_harvest_at(r: aioredis.Redis) -> float | None:
    """When the next harvest attempt is due, or None when the queue is empty."""
    rows = await r.zrange(PENDING_HARVEST_KEY, 0, 0, withscores=True)
    return float(rows[0][1]) if rows else None


async def pending_harvest_depth(r: aioredis.Redis) -> int:
    """How many finished jobs are waiting for their results to be stored."""
    return int(await r.zcard(PENDING_HARVEST_KEY))


async def reschedule_harvest(r: aioredis.Redis, job_id: str, delay_s: float) -> None:
    """Back off a failed harvest: bump its attempt count, retry it after ``delay_s``."""
    async with r.pipeline(transaction=True) as pipe:
        pipe.hincrby(f"job:{job_id}", "harvest_attempts", 1)
        pipe.zadd(PENDING_HARVEST_KEY, {job_id: time.time() + delay_s})
        await pipe.execute()


async def forget_harvests(r: aioredis.Redis, job_ids: list[str]) -> None:
    """Drop harvest entries whose job state expired; their DB row is reaped separately.
    Without this, a hash that vanished while its queue entry lived would be retried
    forever, since every pass sees it as due and can't fetch its metadata."""
    if job_ids:
        await r.zrem(PENDING_HARVEST_KEY, *job_ids)


async def pending_harvest_members(r: aioredis.Redis, job_ids: list[str]) -> set[str]:
    """Which of ``job_ids`` are waiting to be harvested (the reaper's backstop)."""
    if not job_ids:
        return set()
    scores = await r.zmscore(PENDING_HARVEST_KEY, job_ids)
    return {job_id for job_id, score in zip(job_ids, scores, strict=True) if score is not None}


async def set_done(
    r: aioredis.Redis,
    job_id: str,
    state_ttl: int,
    meta: dict[str, str] | None = None,
) -> BatchCounts | None:
    """Mark the job done and drop it from the harvest queue. For a page of a batch,
    returns the batch's new counts, for the caller to publish as a batch event.

    The engine slot is normally released earlier, by ``release_done`` at engine-done;
    the release here is the idempotent fallback for a direct call. ``meta`` is the
    caller's snapshot of the job hash. A batch page still re-reads the hash, because a
    re-run must see the finished status to avoid counting the page twice."""
    key = f"job:{job_id}"
    if meta is None or meta.get("batch_id"):
        meta = await get_job(r, job_id)
    batch = await _count_batch_page(r, meta, "done")
    backend_id = meta.get("backend_id") if meta else None
    # The status write and the slot release don't depend on each other, so send them in
    # one round trip; only the SREM result decides the counters after it.
    async with r.pipeline(transaction=False) as pipe:
        pipe.hset(
            key,
            mapping={"status": "done", "finished_at": str(time.time())},
        )
        pipe.expire(key, state_ttl)
        pipe.srem("jobs:inflight", job_id)
        pipe.zrem(PENDING_HARVEST_KEY, job_id)
        pipe.hdel(key, "harvest_times", "harvest_attempts")
        _, _, removed, _, _ = await pipe.execute()
    if not removed:
        return batch
    # Only decrement when srem actually removed the job: makes slot-release idempotent,
    # so a re-run (e.g. reaper re-processing after a failed commit) can't double-decrement
    # the backend counter.
    async with r.pipeline(transaction=False) as pipe:
        if meta is not None:
            pipe.hincrby(LOAD_INFLIGHT_KEY, _job_load_field(meta), -1)
        if backend_id:
            pipe.decr(f"backend:{backend_id}:inflight")
            # A slot just freed — let the submit worker refill it now.
            pipe.rpush(_SUBMIT_WAKEUP_KEY, b"1")
            pipe.expire(_SUBMIT_WAKEUP_KEY, _SUBMIT_WAKEUP_TTL_SECONDS)
        await pipe.execute()
    if backend_id:
        await _release_batch(r, backend_id, meta)
    return batch


async def set_failed(
    r: aioredis.Redis, job_id: str, error: str, state_ttl: int
) -> BatchCounts | None:
    """Mark the job failed and release its slot; like ``set_done``, returns a batch
    page's new batch counts."""
    key = f"job:{job_id}"
    meta = await get_job(r, job_id)
    batch = await _count_batch_page(r, meta, "failed")
    backend_id = meta.get("backend_id") if meta else None
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(
            key,
            mapping={"status": "failed", "error": error, "finished_at": str(time.time())},
        )
        pipe.expire(key, state_ttl)
        # A harvest that failed for good leaves the durable queue; a poll failure was
        # never on it.
        pipe.zrem(PENDING_HARVEST_KEY, job_id)
        pipe.hdel(key, "harvest_times", "harvest_attempts")
        await pipe.execute()
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
        await _release_batch(r, backend_id, meta)
        await signal_submit(r)  # a slot just freed — let the worker refill it now
    return batch


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
        await _release_batch(r, backend_id, meta)
    await r.hset(key, mapping={"status": "queued"})
    await r.hdel(
        key,
        "engine_job_id",
        "backend_url",
        "backend_id",
        "harvest_times",
        "harvest_attempts",
    )
    await r.zrem(PENDING_HARVEST_KEY, job_id)
    await r.expire(key, state_ttl)

    if delay_s > 0:
        await schedule_retry(r, job_id, score, delay_s, avoid_backend_id)
    else:
        priority = int(meta.get("priority", 0)) if meta else 0
        await _add_pending(r, priority, job_id, score)


async def rebuild_load_counters(r: aioredis.Redis) -> None:
    """Recount the load counters from the queues themselves and swap them in (D3).

    The counters move in separate steps from the queues, so a crash between the two
    leaves them off for good; this repairs it. Sources: every pending ZSET (via the
    levels set, no SCAN), the delayed-retry ZSET and the in-flight set. A transition that
    lands mid-rebuild, or a job the submit worker holds between claim and dispatch, can
    be off by one until the next rebuild, which is fine for an autoscaling signal.
    """
    levels = await r.smembers(_PENDING_LEVELS_KEY)
    queued_ids: list[str] = []
    for level in levels:
        prio = int(level.decode() if isinstance(level, bytes) else level)
        queued_ids += [
            _s(m) for m in cast("list[bytes]", await r.zrange(_pending_key(prio), 0, -1))
        ]
    queued_ids += [_s(m) for m in cast("list[bytes]", await r.zrange(_DELAYED_KEY, 0, -1))]
    inflight_ids = [_s(m) for m in await r.smembers("jobs:inflight")]

    async def count(job_ids: list[str]) -> dict[str, int]:
        async with r.pipeline(transaction=False) as pipe:
            for job_id in job_ids:
                pipe.hgetall(f"job:{job_id}")
            hashes = await pipe.execute()
        counts: dict[str, int] = {}
        for raw in hashes:
            if raw:
                field = _job_load_field({_s(k): _s(v) for k, v in raw.items()})
                counts[field] = counts.get(field, 0) + 1
        return counts

    for key, counts in (
        (LOAD_QUEUED_KEY, await count(queued_ids)),
        (LOAD_INFLIGHT_KEY, await count(inflight_ids)),
    ):
        if not counts:
            await r.delete(key)
            continue
        tmp = f"{key}:rebuild"
        async with r.pipeline(transaction=True) as pipe:
            pipe.delete(tmp)
            pipe.hset(tmp, mapping=cast("dict[Any, Any]", counts))
            pipe.rename(tmp, key)  # atomic swap: readers never see a half-built hash
            await pipe.execute()


# Recount every backend's in-flight jobs and batch pages from the in-flight set, in one
# script so no dispatch or harvest lands between the count and the write.
# ARGV: the backend ids that have counters now (zeroed unless something is in flight).
_REBUILD_BACKEND_COUNTERS_LUA = """
local inflight, batches = {}, {}
for _, id in ipairs(redis.call('SMEMBERS', 'jobs:inflight')) do
  local f = redis.call('HMGET', 'job:' .. id, 'backend_id', 'batch_id')
  if f[1] then
    inflight[f[1]] = (inflight[f[1]] or 0) + 1
    if f[2] and f[2] ~= '' then
      batches[f[1]] = batches[f[1]] or {}
      batches[f[1]][f[2]] = (batches[f[1]][f[2]] or 0) + 1
    end
  end
end
for _, b in ipairs(ARGV) do
  if not inflight[b] then inflight[b] = 0 end
end
local fixed = 0
for b, n in pairs(inflight) do
  local key = 'backend:' .. b .. ':inflight'
  if tonumber(redis.call('GET', key) or '0') ~= n then fixed = fixed + 1 end
  redis.call('SET', key, n)
  local bkey = 'backend:' .. b .. ':batches'
  redis.call('DEL', bkey)
  for batch, count in pairs(batches[b] or {}) do redis.call('HSET', bkey, batch, count) end
end
return fixed
"""


async def rebuild_backend_counters(r: aioredis.Redis) -> int:
    """Recount each backend's in-flight slots (and batch pages, for affinity) from the
    in-flight jobs. The counters move in their own steps, so a crash or a duplicated
    dispatch leaves one off, and nothing else ever corrects it: a count too high loses
    that backend a slot for good, one too low over-dispatches into 503s. Returns how many
    backends were off."""
    backend_ids: set[str] = set()
    async for key in r.scan_iter(match="backend:*:inflight", count=500):
        backend_ids.add(_s(key).split(":")[1])
    sha = await r.script_load(_REBUILD_BACKEND_COUNTERS_LUA)
    fixed = await r.evalsha(sha, 0, *sorted(backend_ids))
    return int(fixed)


async def record_page_seconds(r: aioredis.Redis, domain: str, seconds: float) -> None:
    """Fold one finished page's engine run time into the domain's seconds-per-page
    average. Two pollers racing can drop a sample; for an average that's harmless."""
    prev = await r.hget(LOAD_SPP_KEY, domain)
    value = ewma(float(prev) if prev is not None else None, seconds)
    await r.hset(LOAD_SPP_KEY, domain, str(value))


async def get_inflight_ids(r: aioredis.Redis) -> set[str]:
    members = await r.smembers("jobs:inflight")
    return {m.decode() if isinstance(m, bytes) else m for m in members}


def _backend_batches_key(backend_id: int | str) -> str:
    return f"backend:{backend_id}:batches"


async def _release_batch(
    r: aioredis.Redis, backend_id: int | str, meta: dict[str, str] | None
) -> None:
    """One page of a batch left the backend; drop the batch when it was the last. Only
    called when the job really left the in-flight set, so the count can't go negative."""
    batch_id = meta.get("batch_id") if meta else None
    if not batch_id:
        return
    left = await r.hincrby(_backend_batches_key(backend_id), batch_id, -1)
    if left <= 0:
        await r.hdel(_backend_batches_key(backend_id), batch_id)


async def get_backend_batches(r: aioredis.Redis, backend_id: int) -> dict[str, int]:
    """batch id → pages of it in flight on the backend."""
    raw = await r.hgetall(_backend_batches_key(backend_id))
    return {_s(k): int(v) for k, v in raw.items()}


async def get_backend_inflight(r: aioredis.Redis, backend_id: int) -> int:
    val = await r.get(f"backend:{backend_id}:inflight")
    return int(val) if val else 0


async def get_backends_state(
    r: aioredis.Redis, backend_ids: list[int]
) -> dict[int, tuple[int, dict[str, int]]]:
    """Read every backend's free-slot and batch-affinity state in one Redis trip."""
    if not backend_ids:
        return {}
    async with r.pipeline(transaction=False) as pipe:
        pipe.mget([f"backend:{backend_id}:inflight" for backend_id in backend_ids])
        for backend_id in backend_ids:
            pipe.hgetall(_backend_batches_key(backend_id))
        inflight_values, *batch_values = await pipe.execute()
    return {
        backend_id: (
            int(inflight) if inflight else 0,
            {_s(batch_id): int(count) for batch_id, count in batches.items()},
        )
        for backend_id, inflight, batches in zip(
            backend_ids, inflight_values, batch_values, strict=True
        )
    }


def job_event_payload(
    job_id: str,
    external_id: str | None,
    status: str,
    *,
    batch_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A job event as clients receive it on the WebSocket. ``job_id`` is what they match
    on (submit returned it); ``external_id`` is their own label, or null. A page of a
    batch also carries ``batch_id`` (D12): clients get its results with the batch."""
    event = {"job_id": job_id, "external_id": external_id or None, "status": status, **fields}
    if batch_id:
        event["batch_id"] = batch_id
    return event


async def publish_event(r: aioredis.Redis, username: str, event: dict[str, Any]) -> None:
    channel = f"job:{username}:events"
    await r.publish(channel, json.dumps(event))
