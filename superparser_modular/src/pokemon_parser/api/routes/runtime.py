from __future__ import annotations

from fastapi import APIRouter
from fastapi import Query

from pokemon_parser.api.schemas import ActionModeUpdateRequest, TimerUpdateRequest
from pokemon_parser.api.services.shared import get_logs_manager
from pokemon_parser.api.services.shared import get_runtime_manager

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("/status")
def get_runtime_status():
    return get_runtime_manager().status()


@router.post("/run")
def run_runtime():
    return get_runtime_manager().start()


@router.post("/stop")
def stop_runtime():
    return get_runtime_manager().stop()


@router.post("/restart")
def restart_runtime():
    return get_runtime_manager().restart()


@router.get("/overview")
def get_runtime_overview():
    return get_runtime_manager().build_overview()


@router.get("/debug-logs")
def get_runtime_debug_logs(lines: int = Query(default=200, ge=1, le=1000)):
    return get_logs_manager().debug_files(lines=lines)


@router.get("/timer")
def get_runtime_timer():
    return get_runtime_manager().get_timer_status()


@router.post("/timer")
def update_runtime_timer(payload: TimerUpdateRequest):
    return get_runtime_manager().update_timer(payload.model_dump())


@router.get("/action-mode")
def get_action_mode():
    return get_runtime_manager().config_manager.get_action_mode_settings()


@router.post("/action-mode")
def update_action_mode(payload: ActionModeUpdateRequest):
    return get_runtime_manager().update_action_mode(payload.mode)
