import json

import redis.asyncio as aioredis


class CompatState:
    def __init__(self, r: aioredis.Redis, ttl: int):
        self.r = r
        self.ttl = ttl

    async def create_request(
        self, request_id: str, engine: int, filenames: list[str]
    ) -> None:
        key = f"compat:{request_id}"
        await self.r.hset(
            key,
            mapping={
                "engine": str(engine),
                "filenames": json.dumps(filenames),
            },
        )
        await self.r.expire(key, self.ttl)

    async def get_request(self, request_id: str) -> dict | None:
        key = f"compat:{request_id}"
        data = await self.r.hgetall(key)
        if not data:
            return None
        engine = data.get(b"engine", data.get("engine"))
        filenames = data.get(b"filenames", data.get("filenames"))
        if isinstance(engine, bytes):
            engine = engine.decode()
        if isinstance(filenames, bytes):
            filenames = filenames.decode()
        return {"engine": int(engine), "filenames": json.loads(filenames)}

    async def set_job_id(
        self, request_id: str, filename: str, job_id: str
    ) -> None:
        key = f"compat:{request_id}:{filename}"
        await self.r.set(key, job_id, ex=self.ttl)

    async def get_job_id(self, request_id: str, filename: str) -> str | None:
        key = f"compat:{request_id}:{filename}"
        val = await self.r.get(key)
        if val and isinstance(val, bytes):
            return val.decode()
        return val

    async def get_all_job_ids(self, request_id: str) -> dict[str, str | None]:
        req = await self.get_request(request_id)
        if not req:
            return {}
        result = {}
        for filename in req["filenames"]:
            result[filename] = await self.get_job_id(request_id, filename)
        return result
