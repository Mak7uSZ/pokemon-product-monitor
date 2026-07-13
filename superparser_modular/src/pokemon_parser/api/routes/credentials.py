from __future__ import annotations

from fastapi import APIRouter

from pokemon_parser.api.schemas import CredentialsSaveRequest
from pokemon_parser.api.services.shared import get_config_manager, get_runtime_manager

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


@router.get("")
def get_credentials():
    return get_config_manager().get_credentials()


@router.post("")
def save_credentials(payload: CredentialsSaveRequest):
    response = get_config_manager().save_credentials(payload.values)
    response["runtime_restarted"] = get_runtime_manager().restart_if_running(reason="credentials updated")
    return response


@router.post("/reload")
def reload_credentials():
    return get_config_manager().reload_credentials()
