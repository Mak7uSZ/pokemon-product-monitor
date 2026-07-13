from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from pokemon_parser.api.services.shared import get_config_manager, get_paths
from pokemon_parser.storage.migrations import LATEST_SCHEMA_VERSION

router = APIRouter(prefix="/health", tags=["health"])


def readiness_snapshot(*, database_path: Path, frontend_index: Path) -> tuple[bool, dict]:
    checks: dict[str, dict] = {
        "database": {"ok": False},
        "frontend": {"ok": frontend_index.is_file()},
    }
    try:
        resolved = database_path.resolve()
        connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only = ON")
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            connection.close()
        checks["database"] = {
            "ok": quick_check.lower() == "ok" and schema_version == LATEST_SCHEMA_VERSION,
            "schema_version": schema_version,
            "expected_schema_version": LATEST_SCHEMA_VERSION,
            "integrity": quick_check,
        }
    except Exception as exc:
        checks["database"] = {
            "ok": False,
            "error": type(exc).__name__,
            "expected_schema_version": LATEST_SCHEMA_VERSION,
        }
    ready = all(check["ok"] for check in checks.values())
    return ready, {"ok": ready, "status": "ready" if ready else "not_ready", "checks": checks}


@router.get("/live")
def live():
    return {"ok": True, "status": "alive"}


@router.get("/ready")
def ready():
    cfg = get_config_manager().load_app_config()
    is_ready, payload = readiness_snapshot(
        database_path=cfg.resolved_db_path(),
        frontend_index=get_paths().frontend_dist / "index.html",
    )
    if not is_ready:
        return JSONResponse(status_code=503, content=payload)
    return payload
