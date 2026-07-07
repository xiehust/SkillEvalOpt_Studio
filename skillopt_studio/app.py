"""FastAPI application factory for SkillOpt Studio."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from skillopt_studio.config import StudioConfig
from skillopt_studio.jobs import JobManager

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"


def create_app(config: StudioConfig | None = None) -> FastAPI:
    config = config or StudioConfig.from_env()
    app = FastAPI(title="SkillOpt Studio", version="0.1.0")
    app.state.config = config
    app.state.jobs = JobManager(config)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/environment")
    def environment() -> dict:
        """CLI availability for each exec backend — the wizards surface this
        so a missing `claude`/`codex` binary is flagged before submitting."""
        from skillopt_studio import runners

        backends = []
        for backend, cli in runners.EXEC_BACKENDS.items():
            path = runners.cli_path(backend)
            backends.append(
                {"backend": backend, "cli": cli, "available": path is not None, "path": path}
            )
        return {"backends": backends}

    from skillopt_studio.api import dashboard as dashboard_api
    from skillopt_studio.api import jobs as jobs_api
    from skillopt_studio.api import skills as skills_api
    from skillopt_studio.api import tasksets as tasksets_api

    app.include_router(skills_api.router, prefix="/api")
    app.include_router(tasksets_api.router, prefix="/api")
    app.include_router(jobs_api.router, prefix="/api")
    app.include_router(dashboard_api.router, prefix="/api")

    if FRONTEND_DIST.is_dir():
        _mount_frontend(app, FRONTEND_DIST)

    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA: real files as-is, everything else → index.html
    so client-side routes like /skills survive a hard refresh."""
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index_html = dist / "index.html"
    dist_resolved = dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        if full_path:
            candidate = (dist_resolved / full_path).resolve()
            if dist_resolved in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index_html)
