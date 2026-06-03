from datetime import datetime, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.deps import get_settings
from app.models.db import async_session
from app.models.job import Job
from app.models.user import User
from app.services.auth import hash_key
from app.services.redis_jobs import check_rate_limit

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    settings = get_settings()
    api_key = ws.query_params.get("api_key")
    if not api_key:
        await ws.close(code=1008, reason="Missing api_key")
        return

    # Authenticate
    hashed = hash_key(api_key)
    async with async_session() as db:
        result = await db.execute(
            select(User).where(User.hashed_key == hashed, User.active == True)  # noqa: E712
        )
        user = result.scalar_one_or_none()
        if not user:
            await ws.close(code=1008, reason="Invalid API key")
            return
        username = user.username

    # Rate limit check
    r = aioredis.from_url(settings.redis_url, decode_responses=False)
    try:
        allowed = await check_rate_limit(
            r, f"rl:ws:{username}", settings.rate_limit_ws_connects_per_minute
        )
        if not allowed:
            await ws.close(code=1008, reason="Rate limit exceeded")
            return

        await ws.accept()

        # Catch-up: send recent done/failed events
        async with async_session() as db:
            cutoff = datetime.utcnow() - timedelta(seconds=settings.ws_catch_up_seconds)
            result = await db.execute(
                select(Job).where(
                    Job.username == username,
                    Job.finished_at >= cutoff,
                    Job.status.in_(["done", "failed"]),
                )
            )
            for job in result.scalars().all():
                event = {
                    "uuid": str(job.external_id),
                    "status": job.status,
                    "ts": job.finished_at.isoformat() if job.finished_at else None,
                }
                if job.error:
                    event["error"] = job.error
                await ws.send_json(event)

        # Subscribe to Redis pub/sub
        pubsub = r.pubsub()
        await pubsub.subscribe(f"job:{username}:events")

        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    await ws.send_text(data)
        except WebSocketDisconnect:
            pass
        finally:
            await pubsub.unsubscribe(f"job:{username}:events")
            await pubsub.aclose()
    finally:
        await r.aclose()
