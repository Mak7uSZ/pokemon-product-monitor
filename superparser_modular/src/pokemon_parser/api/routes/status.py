from __future__ import annotations

from fastapi import APIRouter

from pokemon_parser.api.services.shared import get_config_manager

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("/config")
def get_config_status():
    return get_config_manager().get_config_status()
