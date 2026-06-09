from datetime import datetime, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.deps import get_settings
from app.models.db import async_session
from app.models.job import Job
from app.models.user import User
from app.services import config as config_service
from app.services import rate_limit
from app.services.auth import hash_key

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Realtime job-update stream over WebSocket.

    Connect to ``ws://<host>/ws`` (or ``wss://`` behind TLS). This is a
    WebSocket endpoint and therefore does not appear as an interactive
    operation in the Swagger ``/docs`` UI.

    Authentication
    --------------
    Pass the API key as the ``api_key`` query parameter, e.g.
    ``wss://<host>/ws?api_key=<key>``. There is no HTTP body or header auth.
    The connection is closed with code ``1008`` (policy violation) if:

    - the ``api_key`` query parameter is missing (reason ``"Missing api_key"``);
    - the key does not match an active user (reason ``"Invalid API key"``);
    - the per-user rate limit for the ``ws_connect`` action is exceeded
      (reason ``"Rate limit exceeded"``).

    On any of these the socket is closed before ``accept()``, so no messages
    are delivered.

    Message protocol
    ----------------
    Communication is one-way: the **server sends, the client only listens**.
    Any inbound client messages are ignored. Each server message is a single
    JSON object describing one job state transition for the authenticated user.

    Immediately after a successful handshake, the server replays a "catch-up"
    batch: every ``done``/``failed`` job whose ``finished_at`` falls within the
    last ``ws_catch_up_seconds`` (default 120) seconds. Catch-up events have the
    shape::

        {
            "uuid": "<job external id>",   // string
            "status": "done" | "failed",
            "ts": "<ISO-8601 timestamp>",  // finished_at, may be null
            "error": "<message>"           // present only when status == failed
        }

    After the catch-up batch the server subscribes to the per-user Redis
    channel ``job:<username>:events`` and forwards each published event verbatim
    as a text frame (raw JSON string). Live events published by the workers have
    the shape::

        {
            "uuid": "<job external id>",   // string
            "status": "done" | "failed",
            "alto_url": "<presigned URL>", // on "done", only for ALTO results
            "txt_url": "<presigned URL>",  // on "done", only for text results
            "error": "<message>"           // present only when status == failed
        }

    Note the live ``done`` event carries presigned result URLs (one of
    ``alto_url`` / ``txt_url`` depending on the requested format) but no ``ts``
    field, whereas the catch-up replay carries ``ts`` but no result URLs.

    Closing
    -------
    The socket stays open and streams events until the client disconnects
    (``WebSocketDisconnect``); the server then unsubscribes from the channel and
    releases its Redis connection. The server does not close the socket on its
    own once accepted.
    """
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
        async with async_session() as db:
            limits = await config_service.effective_limits(db, username, "ws_connect")
        result = await rate_limit.check(r, "ws_connect", username, limits.per_minute, limits.burst)
        if not result.allowed:
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
