from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from pokemon_parser.api.schemas import DbCleanupRequest, NotificationsUpdateRequest, WorkerSettingsUpdateRequest
from pokemon_parser.api.services.shared import get_config_manager, get_runtime_manager

router = APIRouter(prefix="/api", tags=["settings"])
logger = logging.getLogger(__name__)


@router.get("/notifications")
def get_notifications():
    return get_config_manager().get_notifications_settings()


@router.post("/notifications")
def save_notifications(payload: NotificationsUpdateRequest):
    try:
        response = get_config_manager().save_notifications_settings(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response["runtime_restarted"] = get_runtime_manager().restart_if_running(reason="notification settings updated")
    return response


@router.get("/worker-settings")
def get_worker_settings():
    return get_config_manager().get_worker_settings()


@router.post("/worker-settings")
def save_worker_settings(payload: WorkerSettingsUpdateRequest):
    try:
        response = get_config_manager().save_worker_settings(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response["runtime_restarted"] = get_runtime_manager().restart_if_running(reason="worker settings updated")
    return response


@router.get("/db/status")
def get_db_status():
    return get_runtime_manager().db_status()


@router.post("/db/backup")
def backup_db():
    return get_runtime_manager().backup_db()


@router.post("/db/clear-old-logs")
def clear_old_logs(payload: DbCleanupRequest):
    return get_runtime_manager().clear_old_logs(days=payload.days)


@router.post("/db/clear-stale-actions")
def clear_stale_actions(payload: DbCleanupRequest):
    try:
        return get_runtime_manager().clear_stale_actions(days=payload.days, site=payload.site)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/debug/actions")
def debug_actions(
    site: str | None = None,
    external_id: str | None = None,
    action_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
):
    return get_runtime_manager().debug_actions(site=site, external_id=external_id, action_id=action_id, limit=limit)


@router.post("/chrome-profile/create")
def create_chrome_profile():
    logger.info("chrome_profile_bootstrap_requested")
    if get_runtime_manager().is_running():
        logger.info("chrome_profile_bootstrap_blocked_runtime_running")
        raise HTTPException(
            status_code=409,
            detail="Runtime is running; Chrome profile bootstrap is disabled during runtime.",
        )
    try:
        return get_config_manager().create_chrome_profile()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
