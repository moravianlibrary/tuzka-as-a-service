import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import Settings
from app.deps import get_settings
from app.routers import admin, dashboard, jobs, ws
from app.services import dash_session, storage


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()

    # Fail fast: the API cannot function without these secrets, so refuse to start
    # rather than boot a half-configured, insecure surface.
    missing = [
        name
        for name, value in (
            ("MASTER_KEY", settings.master_key),
            ("KEY_ENCRYPTION_SECRET", settings.key_encryption_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"required config missing: {', '.join(missing)}")

    # One pooled redis client for the app's lifetime (get_redis hands out this shared
    # client), rather than a new pool per request.
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=False)
    app.state.incoming_client = storage.get_incoming_client(settings)
    app.state.results_client = storage.get_results_client(settings)
    app.state.results_public_client = storage.get_results_public_client(settings)
    try:
        yield
    finally:
        await app.state.redis.aclose()


DESCRIPTION = """\
**Tuzka as a Service** — an async OCR gateway in front of the TuzkaOCR engine.

Submit images for OCR, poll for completion, and download results. Operators
manage users, backends and runtime limits through the admin API and dashboard.

- **Client API** (`/api/v1`) — authenticated with an `X-API-Key` header.
- **Admin & Dashboard** — authenticated with the master key.
"""

TAGS_METADATA = [
    {
        "name": "Jobs",
        "description": "Public client API: submit OCR jobs, poll status, and download "
        "results. Authenticated with an `X-API-Key` header.",
    },
    {
        "name": "WebSocket",
        "description": "Realtime job-status stream over WebSocket. Authenticated with an "
        "`api_key` query parameter.",
    },
    {
        "name": "Admin",
        "description": "Manage users, backends and runtime config. Requires the master key.",
    },
    {
        "name": "Dashboard",
        "description": "Read-only stats, usage and backend health powering the dashboard UI, "
        "plus its login/logout. Requires the master key.",
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

    app = FastAPI(
        title="taas",
        version="0.7.0",
        lifespan=lifespan,
        description=DESCRIPTION,
        license_info={"name": "Apache 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
        openapi_tags=TAGS_METADATA,
    )

    app.include_router(jobs.router, prefix="/api/v1", tags=["Jobs"])
    app.include_router(admin.router, prefix="/admin", tags=["Admin"])
    app.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
    app.include_router(ws.router, tags=["WebSocket"])

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    templates = Jinja2Templates(directory=str(static_dir))

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Redirect the bare root to the dashboard."""
        return RedirectResponse(url="/dashboard")

    @app.get("/dashboard", tags=["Dashboard"], summary="Dashboard UI", response_class=HTMLResponse)
    async def dashboard_page(request: Request) -> HTMLResponse:
        """Serve the single-page dashboard UI (HTML)."""
        return templates.TemplateResponse(request, "index.html")

    @app.post(
        "/dashboard/login",
        tags=["Dashboard"],
        summary="Dashboard login",
        responses={401: {"description": "Invalid master key"}},
    )
    async def dashboard_login(
        request: Request,
        response: Response,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        """Exchange the master key (``X-Master-Key`` header) for a short-lived,
        httponly session cookie used by the dashboard."""
        key = request.headers.get("X-Master-Key", "")
        if not key or not hmac.compare_digest(key, settings.master_key):
            raise HTTPException(status_code=401, detail="Invalid master key")
        # Behind a TLS-terminating ingress the app sees http, so trust the
        # forwarded proto to decide whether to mark the cookie Secure.
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        response.set_cookie(
            dash_session.COOKIE_NAME,
            dash_session.issue(settings.master_key),
            max_age=dash_session.DEFAULT_TTL,
            httponly=True,
            samesite="strict",
            secure=(proto == "https"),
            path="/",
        )
        return {"status": "ok"}

    @app.post("/dashboard/logout", tags=["Dashboard"], summary="Dashboard logout")
    async def dashboard_logout(response: Response) -> dict[str, str]:
        """Clear the dashboard session cookie."""
        response.delete_cookie(dash_session.COOKIE_NAME, path="/")
        return {"status": "ok"}

    @app.get("/healthz", tags=["Health"], summary="Liveness probe")
    async def healthz() -> dict[str, str]:
        """Liveness probe — returns ``{"status": "ok"}`` when the app is up."""
        return {"status": "ok"}

    return app


app = create_app()
