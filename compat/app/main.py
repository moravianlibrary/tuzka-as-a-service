from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI

from .config import Settings
from .routers import legacy
from .state import CompatState

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(base_url=settings.taas_base_url)
    app.state.redis = aioredis.from_url(settings.redis_url)
    app.state.compat_state = CompatState(app.state.redis, settings.compat_ttl_seconds)
    app.state.settings = settings
    yield
    await app.state.http.aclose()
    await app.state.redis.aclose()


app = FastAPI(title="taas-compat", lifespan=lifespan)
app.include_router(legacy.router)
