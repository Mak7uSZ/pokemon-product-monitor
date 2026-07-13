from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from pokemon_parser.api.services.shared import get_settings_backup_manager

router = APIRouter(prefix="/api/settings-backup", tags=["settings-backup"])


@router.get("/export")
def export_settings_backup(include_watchlist_items: bool = Query(default=True)):
    manager = get_settings_backup_manager()
    payload = manager.export_backup(include_watchlist_items=include_watchlist_items)
    filename = manager.build_download_filename()
    return Response(
        content=json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/preview")
def preview_settings_restore(payload: dict[str, Any]):
    try:
        return get_settings_backup_manager().preview_restore(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/restore")
def restore_settings_backup(payload: dict[str, Any]):
    try:
        return get_settings_backup_manager().restore(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/snapshot")
def save_settings_snapshot(include_watchlist_items: bool = Query(default=True)):
    return get_settings_backup_manager().save_snapshot(include_watchlist_items=include_watchlist_items)
