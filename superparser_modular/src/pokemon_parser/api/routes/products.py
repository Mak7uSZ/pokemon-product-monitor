from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from pokemon_parser.api.services.shared import get_config_manager, storage_context

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("")
def list_products(
    site: str | None = Query(default=None),
    lifecycle_state: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    cfg = get_config_manager().load_app_config()
    with storage_context(cfg) as storage:
        items = storage.list_products(
            site=site,
            lifecycle_state=lifecycle_state,
            include_archived=include_archived,
            limit=limit,
        )
    return {"ok": True, "items": items}


@router.delete("/{site}/{external_id}")
def archive_product(site: str, external_id: str):
    cfg = get_config_manager().load_app_config()
    with storage_context(cfg) as storage:
        archived = storage.archive_product(site, external_id)
    if not archived:
        raise HTTPException(status_code=404, detail="Product not found or already archived.")
    return {"ok": True, "archived": True}
