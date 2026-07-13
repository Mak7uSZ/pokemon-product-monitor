from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pokemon_parser.api.services.shared import get_parser_settings_manager

router = APIRouter(prefix="/api/parsers", tags=["parsers"])


@router.get("")
def list_parsers():
    return get_parser_settings_manager().list_parsers()


@router.post("/{site}/toggle")
def toggle_parser(site: str):
    try:
        return get_parser_settings_manager().toggle_parser(site)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
