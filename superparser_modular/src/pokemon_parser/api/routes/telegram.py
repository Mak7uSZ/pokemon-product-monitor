from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pokemon_parser.api.schemas import TelegramTestRequest, TelegramUpdateRequest
from pokemon_parser.api.services.shared import get_config_manager, get_runtime_manager

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.get("")
def get_telegram_settings():
    return get_config_manager().get_telegram_settings()


@router.post("")
def save_telegram_settings(payload: TelegramUpdateRequest):
    response = get_config_manager().save_telegram_settings(payload.model_dump())
    response["runtime_restarted"] = get_runtime_manager().restart_if_running(reason="telegram settings updated")
    return response


@router.post("/test")
def send_telegram_test(payload: TelegramTestRequest):
    try:
        return get_config_manager().send_telegram_test(payload.text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
