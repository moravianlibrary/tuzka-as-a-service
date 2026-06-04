from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import Settings
from app.deps import get_settings
from app.routers import admin, dashboard, jobs, ws
from app.services import dash_session


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="taas", version="0.1.0", lifespan=lifespan)

    app.include_router(jobs.router, prefix="/api/v1")
    app.include_router(admin.router, prefix="/admin")
    app.include_router(dashboard.router, prefix="/dashboard")
    app.include_router(ws.router)

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    templates = Jinja2Templates(directory=str(static_dir))

    @app.get("/dashboard")
    async def dashboard_page(request: Request):
        return templates.TemplateResponse(request, "index.html")

    @app.post("/dashboard/login")
    async def dashboard_login(
        request: Request,
        response: Response,
        settings: Settings = Depends(get_settings),
    ):
        key = request.headers.get("X-Master-Key", "")
        if not key or key != settings.master_key:
            raise HTTPException(status_code=403, detail="Invalid master key")
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

    @app.post("/dashboard/logout")
    async def dashboard_logout(response: Response):
        response.delete_cookie(dash_session.COOKIE_NAME, path="/")
        return {"status": "ok"}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()
