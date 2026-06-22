"""FastAPI app factory — mounts all routes."""

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from fitnessbot.config import Config
from fitnessbot.web import admin, auth, dashboard, profile, health_data, connections


def create_app() -> FastAPI:
    app = FastAPI(title="Fitness & Health Intelligence Platform")

    # Static files
    app.mount("/static", StaticFiles(directory=str(Config.STATIC_DIR)), name="static")

    # Include route modules
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(profile.router)
    app.include_router(health_data.router)
    app.include_router(connections.router)
    app.include_router(admin.router)

    @app.get("/")
    async def root(request: Request):
        user = auth.get_current_user(request)
        if user:
            return RedirectResponse("/dashboard", status_code=303)
        return RedirectResponse("/login", status_code=303)

    return app
