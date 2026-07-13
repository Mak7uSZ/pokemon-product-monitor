from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pokemon_parser.api.schemas import WatchlistManualRequest, WatchlistPatchRequest, WatchlistSyncRequest
from pokemon_parser.api.services.shared import get_watchlist_manager

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get("")
def list_watchlist():
    return get_watchlist_manager().list_items()


@router.get("/summary")
def watchlist_summary():
    return get_watchlist_manager().summary()


@router.get("/state")
def watchlist_state():
    return get_watchlist_manager().state()


@router.post("/build-from-filters")
def build_watchlist_from_filters(payload: WatchlistSyncRequest | None = None):
    return get_watchlist_manager().build_from_filters(site=payload.site if payload else None)


@router.post("/sync-now")
def sync_watchlist_now(payload: WatchlistSyncRequest | None = None):
    return get_watchlist_manager().scan_now(
        site=payload.site if payload else None,
        product_key=payload.product_key if payload else None,
    )


@router.post("/{item_id}/sync-now")
def sync_watchlist_item_now(item_id: int):
    result = get_watchlist_manager().scan_now(item_id=item_id)
    if result.get("checked") == 0:
        raise HTTPException(status_code=404, detail="watchlist item not found")
    return result


@router.patch("/{item_id}")
def patch_watchlist_item(item_id: int, payload: WatchlistPatchRequest):
    clean_payload = {key: value for key, value in payload.model_dump().items() if value is not None}
    result = get_watchlist_manager().patch_item(item_id, clean_payload)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "watchlist item not found"))
    return result


@router.delete("/{item_id}")
def delete_watchlist_item(item_id: int):
    result = get_watchlist_manager().delete_item(item_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="watchlist item not found")
    return result


@router.post("/manual")
def add_manual_watchlist_item(payload: WatchlistManualRequest):
    result = get_watchlist_manager().add_manual(payload.model_dump())
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "failed to add manual watchlist item"))
    return result
