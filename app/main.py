import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.config import Settings
from app.models import db
from app.routers import batches, jobs, load, ws
from app.services import storage
from app.services.event_emitter import get_emitter
from app.services.redis_pool import make_redis
from app.telemetry import instrument_app, setup_telemetry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()

    # Fail fast: the API cannot function without these secrets, so refuse to start
    # rather than boot a half-configured, insecure surface.
    missing = [
        name
        for name, value in (("KEY_ENCRYPTION_SECRET", settings.key_encryption_secret),)
        if not value
    ]
    if missing:
        raise RuntimeError(f"required config missing: {', '.join(missing)}")

    # One pooled redis client for the app's lifetime (get_redis hands out this shared
    # client), rather than a new pool per request.
    app.state.redis = make_redis(settings)
    app.state.incoming_client = storage.get_incoming_client(settings)
    app.state.results_client = storage.get_results_client(settings)
    app.state.results_public_client = storage.get_results_public_client(settings)
    emitter = get_emitter(settings)
    await emitter.start()
    try:
        yield
    finally:
        await emitter.aclose()
        await app.state.redis.aclose()


DESCRIPTION = """\
**Tuzka as a Service** — an async OCR gateway in front of the TuzkaOCR engine.

Submit images for OCR, poll for completion, and download results. Operators manage
users, backends and runtime config with the `taas-admin` CLI (in the api image); job
history and dashboards live in ClickStack (HyperDX).

- **Client API** (`/api/v1`) — authenticated with an `X-API-Key` header.
"""

TAGS_METADATA = [
    {
        "name": "Jobs",
        "description": "Public client API: submit OCR jobs, poll status, and download "
        "results. Authenticated with an `X-API-Key` header.",
    },
    {
        "name": "Batches",
        "description": "Many pages at once: one archive in, one archive of results out. "
        "Authenticated with an `X-API-Key` header.",
    },
    {
        "name": "WebSocket",
        "description": "Realtime job-status stream over WebSocket. Authenticated with an "
        "`api_key` query parameter.",
    },
    {"name": "Health", "description": "Liveness probe."},
]


def create_app() -> FastAPI:
    # Uvicorn configures only its own loggers, leaving the root logger unset, so app
    # `logging.getLogger(...)` records (routers, services) were silently dropped.
    # Configure the root logger here — same approach the workers already use — so app
    # logs reach stdout. force=True wins over any prior basicConfig from imports.
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    # httpx emits one INFO line per request; quiet it so it doesn't drown app logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # After basicConfig(force=True), which would drop the OTel log handler.
    setup_telemetry("taas-api", db_engine=db.engine, root_spans=True)

    app = FastAPI(
        title="taas",
        version="0.8.1",
        lifespan=lifespan,
        description=DESCRIPTION,
        license_info={"name": "Apache 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
        openapi_tags=TAGS_METADATA,
    )

    instrument_app(app)

    app.include_router(jobs.router, prefix="/api/v1", tags=["Jobs"])
    app.include_router(batches.router, prefix="/api/v1", tags=["Batches"])
    app.include_router(ws.router, tags=["WebSocket"])
    # Internal autoscaler metric: token-guarded and kept out of the public schema.
    app.include_router(load.router, include_in_schema=False)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Redirect the bare root to the API docs."""
        return RedirectResponse(url="/docs")

    @app.get("/healthz", tags=["Health"], summary="Liveness probe")
    async def healthz() -> dict[str, str]:
        """Liveness probe — returns ``{"status": "ok"}`` when the app is up."""
        return {"status": "ok"}

    return app


app = create_app()
