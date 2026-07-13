from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pokemon_parser.api.schemas import FilterPayload
from pokemon_parser.api.services.shared import get_filters_manager

router = APIRouter(prefix="/api/filters", tags=["filters"])


@router.get("")
def list_filters():
    return get_filters_manager().list_filters()


@router.get("/{filter_id}")
def get_filter(filter_id: int):
    try:
        return get_filters_manager().get_filter(filter_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="filter not found") from exc


@router.post("")
def create_filter(payload: FilterPayload):
    return get_filters_manager().create_filter(payload.model_dump())


@router.put("/{filter_id}")
def update_filter(filter_id: int, payload: FilterPayload):
    try:
        return get_filters_manager().update_filter(filter_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="filter not found") from exc


@router.delete("/{filter_id}")
def delete_filter(filter_id: int):
    return get_filters_manager().delete_filter(filter_id)


@router.post("/{filter_id}/toggle")
def toggle_filter(filter_id: int):
    try:
        return get_filters_manager().toggle_filter(filter_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="filter not found") from exc


@router.post("/import-json")
def import_filters_from_json():
    response = get_filters_manager().import_filters_from_json()
    if not response.get("ok", False):
        raise HTTPException(status_code=400, detail=response.get("message", "Failed to import filters.json"))
    return response
