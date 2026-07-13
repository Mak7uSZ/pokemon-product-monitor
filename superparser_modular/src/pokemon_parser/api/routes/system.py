from __future__ import annotations

from fastapi import APIRouter, Request

from pokemon_parser.api.services.dashboard_clean_slate import build_manual_clean_slate_json_response
from pokemon_parser.api.services.shared import get_runtime_manager

router = APIRouter(prefix="/api/system", tags=["system"])


@router.post("/shutdown")
def shutdown_system():
    return get_runtime_manager().initiate_system_shutdown()


@router.get("/dashboard-clean-slate")
def get_dashboard_clean_slate(request: Request):
    return build_manual_clean_slate_json_response(request, reason="manual")


@router.post("/dashboard-clean-slate")
def post_dashboard_clean_slate(request: Request):
    return build_manual_clean_slate_json_response(request, reason="manual")
