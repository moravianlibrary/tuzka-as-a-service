from dataclasses import dataclass

import redis.asyncio as aioredis
from redis.exceptions import NoScriptError

# GCRA (Generic Cell Rate Algorithm).
# KEYS[1] = TAT key, ARGV[1] = emission interval T (s), ARGV[2] = burst tolerance tau (s).
# Uses the Redis server clock so multiple API replicas agree on "now".
# Returns {allowed (0/1), retry_after (string, seconds)}.
GCRA_LUA = """
local T = tonumber(ARGV[1])
local tau = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local tat = tonumber(redis.call('GET', KEYS[1]))
if tat == nil or tat < now then
  tat = now
end
if tat - now > tau then
  return {0, tostring(tat - tau - now)}
end
redis.call('SET', KEYS[1], tostring(tat + T), 'EX', math.ceil(tau + T) + 1)
return {1, '0'}
"""

_script_sha: str | None = None


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: float


def _as_float(raw) -> float:
    return float(raw.decode() if isinstance(raw, bytes) else raw)


async def check(
    r: aioredis.Redis,
    limit_class: str,
    username: str,
    per_minute: int,
    burst: int,
) -> RateLimitResult:
    """GCRA check: allows burst+1 instant requests, then one per 60/per_minute s."""
    global _script_sha
    # Misconfigured limits (admin-editable) must not 500 the hot path.
    if per_minute <= 0:
        return RateLimitResult(allowed=False, retry_after=60.0)
    burst = max(burst, 0)
    emission_interval = 60.0 / per_minute
    tau = emission_interval * burst
    key = f"rl:{limit_class}:{username}"

    if _script_sha is None:
        _script_sha = await r.script_load(GCRA_LUA)
    try:
        allowed, retry_after = await r.evalsha(_script_sha, 1, key, emission_interval, tau)
    except NoScriptError:
        _script_sha = await r.script_load(GCRA_LUA)
        allowed, retry_after = await r.evalsha(_script_sha, 1, key, emission_interval, tau)

    return RateLimitResult(allowed=bool(int(allowed)), retry_after=_as_float(retry_after))
