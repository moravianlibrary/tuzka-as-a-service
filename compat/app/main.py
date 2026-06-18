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


DESCRIPTION = """\
**taas-compat** — a compatibility shim that exposes the legacy *PERO* OCR HTTP API
and forwards requests to the modern **Tuzka as a Service** backend.

Lets existing PERO clients keep their endpoints and response shapes (errors are
returned as `{"message": ...}`) while OCR runs on taas. Authenticated with an API key.
"""

TAGS_METADATA = [
    {
        "name": "Legacy (PERO compat)",
        "description": "Legacy PERO-compatible endpoints. Each maps onto the modern taas "
        "API; errors use the `{\"message\": ...}` envelope PERO clients expect.",
    },
]

app = FastAPI(
    title="taas-compat",
    version="0.5.2",
    lifespan=lifespan,
    description=DESCRIPTION,
    license_info={"name": "Apache 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
    openapi_tags=TAGS_METADATA,
)


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


app.include_router(legacy.router, tags=["Legacy (PERO compat)"])
