from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pokemon_parser.api.routes import (
    credentials_router,
    filters_router,
    health_router,
    logs_router,
    parsers_router,
    products_router,
    proxy_router,
    runtime_router,
    scan_settings_router,
    settings_backup_router,
    settings_router,
    status_router,
    system_router,
    telegram_router,
    watchlist_router,
)
from pokemon_parser.api.services.dashboard_clean_slate import (
    DASHBOARD_CLEAN_SLATE_HEADER,
    DASHBOARD_CLEAN_SLATE_REASON_HEADER,
    DASHBOARD_COOKIE_BYTES_HEADER,
    DASHBOARD_HEADER_BYTES_HEADER,
    build_clean_slate_html_response,
    build_clean_slate_json_response,
    dashboard_clean_slate_reason,
)
from pokemon_parser.api.services.shared import (
    get_config_manager,
    get_paths,
    get_runtime_manager,
    storage_context,
)
from pokemon_parser.utils.logging_setup import setup_debug_logging


logger = logging.getLogger(__name__)


def safe_frontend_file(frontend_dist: Path, requested_path: str) -> Path | None:
    """Resolve a requested SPA asset without allowing traversal or symlink escape."""
    root = frontend_dist.resolve()
    decoded = str(requested_path or "")
    for _ in range(2):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    decoded = decoded.replace("\\", "/").lstrip("/")
    try:
        candidate = (root / decoded).resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def create_app() -> FastAPI:
    paths = get_paths()
    setup_debug_logging(paths.app_root)

    app = FastAPI(title="Pokemon Parser Dashboard API", version="2.0.0")
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            DASHBOARD_CLEAN_SLATE_HEADER,
            DASHBOARD_CLEAN_SLATE_REASON_HEADER,
            DASHBOARD_COOKIE_BYTES_HEADER,
            DASHBOARD_HEADER_BYTES_HEADER,
        ],
    )

    @app.middleware("http")
    async def dashboard_clean_slate_middleware(request: Request, call_next):
        reason = dashboard_clean_slate_reason(request)
        if reason:
            return build_clean_slate_json_response(request, reason=reason)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(runtime_router)
    app.include_router(health_router)
    app.include_router(parsers_router)
    app.include_router(products_router)
    app.include_router(filters_router)
    app.include_router(credentials_router)
    app.include_router(logs_router)
    app.include_router(status_router)
    app.include_router(proxy_router)
    app.include_router(telegram_router)
    app.include_router(scan_settings_router)
    app.include_router(settings_backup_router)
    app.include_router(settings_router)
    app.include_router(system_router)
    app.include_router(watchlist_router)

    if paths.frontend_dist.exists():
        assets_dir = paths.frontend_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/clean-slate", include_in_schema=False)
        def serve_clean_slate(request: Request):
            return build_clean_slate_html_response(request, reason="dashboard_restart")

        @app.get("/", include_in_schema=False)
        def serve_index():
            return FileResponse(paths.frontend_dist / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_frontend(full_path: str):
            candidate = safe_frontend_file(paths.frontend_dist, full_path)
            if candidate is not None:
                return FileResponse(candidate)
            return FileResponse(paths.frontend_dist / "index.html")

    @app.on_event("startup")
    def on_startup() -> None:
        # Finish schema creation/migration before the service can report ready.
        # This also creates the verified pre-migration backup for legacy data.
        logger.info(
            "application startup database preparation started; large legacy databases may take several minutes"
        )
        with storage_context(get_config_manager().load_app_config()):
            pass
        logger.info("application startup database preparation complete")
        get_runtime_manager()

    @app.on_event("shutdown")
    def on_shutdown() -> None:
        get_runtime_manager().close()

    return app


app = create_app()
