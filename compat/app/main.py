from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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


@app.exception_handler(StarletteHTTPException)
async def message_envelope(request: Request, exc: StarletteHTTPException):
    # The legacy PERO API (and its clients) expect errors as {"message": ...},
    # not FastAPI's default {"detail": ...}. The client's polling loop reads
    # response.json()["message"] to detect "not processed yet", so this
    # envelope must match.
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.detail},
        headers=exc.headers,
    )


app.include_router(legacy.router)
