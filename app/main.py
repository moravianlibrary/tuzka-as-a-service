from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.routers import admin, dashboard, jobs, ws


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

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()
