from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Query

from pokemon_parser.api.services.shared import get_logs_manager

router = APIRouter(prefix="/api/logs", tags=["logs"])
logger = logging.getLogger(__name__)


@router.get("")
def list_logs(
    type: str | None = Query(default=None),
    site: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    return get_logs_manager().list_logs(log_type=type, site=site, query=q, limit=limit)


@router.get("/summary")
def get_logs_summary():
    started = time.perf_counter()
    summary = get_logs_manager().summary()
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "/api/logs/summary responded in %.1f ms cached=%s stale=%s",
        duration_ms,
        summary.get("cached"),
        summary.get("stale"),
    )
    return summary


@router.get("/debug")
def get_debug_logs(lines: int = Query(default=200, ge=1, le=1000)):
    return get_logs_manager().debug_files(lines=lines)
