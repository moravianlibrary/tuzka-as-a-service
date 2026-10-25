"""Redis-backed store mapping legacy request IDs to engine and filename metadata."""

from typing import Any, cast

import redis.asyncio as aioredis

from .schemas import CompatRequest


class CompatState:
    def __init__(self, r: aioredis.Redis, ttl: int) -> None:
        self.r = r
        self.ttl = ttl

    async def create_request(self, request_id: str, engine: int, filenames: list[str]) -> None:
        key = f"compat:{request_id}"
        request = CompatRequest(engine=engine, filenames=filenames)
        await self.r.hset(key, mapping=cast("dict[Any, Any]", request.to_hash()))
        await self.r.expire(key, self.ttl)

    async def get_request(self, request_id: str) -> CompatRequest | None:
        data = await self.r.hgetall(f"compat:{request_id}")
        return CompatRequest.from_hash(data) if data else None

    async def set_job_id(self, request_id: str, filename: str, job_id: str) -> None:
        key = f"compat:{request_id}:{filename}"
        await self.r.set(key, job_id, ex=self.ttl)

    async def get_job_id(self, request_id: str, filename: str) -> str | None:
        key = f"compat:{request_id}:{filename}"
        val: Any = await self.r.get(key)
        if val and isinstance(val, bytes):
            return val.decode()
        return cast("str | None", val)

    async def get_all_job_ids(self, request_id: str) -> dict[str, str | None]:
        req = await self.get_request(request_id)
        if not req:
            return {}
        result: dict[str, str | None] = {}
        for filename in req.filenames:
            result[filename] = await self.get_job_id(request_id, filename)
        return result
