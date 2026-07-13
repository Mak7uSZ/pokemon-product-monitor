from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pokemon_parser.api.schemas import ProxyUpdateRequest
from pokemon_parser.api.services.shared import get_config_manager, get_runtime_manager

router = APIRouter(prefix="/api/proxy", tags=["proxy"])


@router.get("")
def get_proxy_settings():
    return get_config_manager().get_proxy_settings()


@router.post("")
def save_proxy_settings(payload: ProxyUpdateRequest):
    response = get_config_manager().save_proxy_settings(payload.model_dump(by_alias=True))
    response["runtime_restarted"] = get_runtime_manager().restart_if_running(reason="proxy settings updated")
    return response


@router.post("/test")
def test_proxy(payload: ProxyUpdateRequest | None = None):
    try:
        candidate = payload.model_dump(by_alias=True) if payload is not None else None
        return get_config_manager().test_proxy(candidate)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
