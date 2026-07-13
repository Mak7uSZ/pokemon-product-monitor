from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pokemon_parser.api.schemas import ScanSettingsUpdateRequest
from pokemon_parser.api.services.shared import get_config_manager, get_runtime_manager

router = APIRouter(prefix="/api/scan-settings", tags=["scan-settings"])


@router.get("")
def get_scan_settings():
    return get_config_manager().get_scan_settings()


@router.get("/effective")
def get_effective_scan_settings():
    return get_config_manager().get_scan_settings_effective()


@router.post("")
def save_scan_settings(payload: ScanSettingsUpdateRequest):
    try:
        response = get_config_manager().save_scan_settings(payload.model_dump(by_alias=True))
        response["runtime_restarted"] = get_runtime_manager().restart_if_running(reason="scan settings updated")
        return response
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reset-defaults")
def reset_scan_settings_defaults():
    response = get_config_manager().reset_scan_settings_defaults()
    response["runtime_restarted"] = get_runtime_manager().restart_if_running(reason="scan settings reset")
    return response
