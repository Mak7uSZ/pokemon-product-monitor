from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import aiohttp

from pokemon_parser.api.services.config_manager import ConfigManager
from pokemon_parser.engine.watchlist import WatchlistTracker
from pokemon_parser.models import WatchlistProduct
from pokemon_parser.notifications.telegram import TelegramNotifier
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.utils.proxy import ProxyAwareSession
from pokemon_parser.utils.url_safety import validate_retailer_url


class WatchlistManager:
    def __init__(self, *, config_manager: ConfigManager, runtime_manager: Any | None = None) -> None:
        self.config_manager = config_manager
        self.runtime_manager = runtime_manager

    def _storage(self) -> tuple[sqlite3.Connection, SqliteStorage]:
        cfg = self.config_manager.load_app_config()
        conn = sqlite3.connect(str(cfg.resolved_db_path()), check_same_thread=False)
        storage = SqliteStorage(conn)
        storage.init_schema()
        return conn, storage

    def list_items(self) -> dict[str, Any]:
        conn, storage = self._storage()
        try:
            return {"ok": True, "items": storage.list_watchlist(limit=2000)}
        finally:
            conn.close()

    def summary(self) -> dict[str, Any]:
        conn, storage = self._storage()
        try:
            return {"ok": True, **storage.watchlist_summary()}
        finally:
            conn.close()

    def state(self) -> dict[str, Any]:
        conn, storage = self._storage()
        try:
            return {"ok": True, **storage.watchlist_state(limit=2000)}
        finally:
            conn.close()

    def patch_item(self, item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        conn, storage = self._storage()
        try:
            updated = storage.update_watchlist_item(item_id, payload)
            if updated is None:
                return {"ok": False, "message": "not_found"}
            return {"ok": True, "item": updated}
        finally:
            conn.close()

    def delete_item(self, item_id: int) -> dict[str, Any]:
        conn, storage = self._storage()
        try:
            return {"ok": storage.delete_watchlist_item(item_id)}
        finally:
            conn.close()

    def add_manual(self, payload: dict[str, Any]) -> dict[str, Any]:
        conn, storage = self._storage()
        try:
            site = str(payload.get("site") or "").strip().lower()
            product_key = str(payload.get("product_key") or payload.get("sku") or payload.get("article_number") or "").strip()
            url = str(payload.get("url") or "").strip()
            if not site or not product_key or not url:
                return {"ok": False, "message": "site, product_key/sku, and url are required"}
            try:
                url = validate_retailer_url(site, url)
            except ValueError as exc:
                return {"ok": False, "message": str(exc)}
            item = storage.upsert_watchlist_entry(
                WatchlistProduct(
                    site=site,
                    product_key=product_key,
                    article_number=payload.get("article_number") or payload.get("sku"),
                    sku=payload.get("sku"),
                    handle=payload.get("handle"),
                    title=str(payload.get("title") or product_key),
                    url=url,
                    pinned=bool(payload.get("pinned", True)),
                    source="manual",
                    current_inventory_status="unknown",
                    status_confidence_score=0.0,
                )
            )
            return {"ok": True, "item": item}
        finally:
            conn.close()

    def build_from_filters(self, *, site: str | None = None) -> dict[str, Any]:
        return asyncio.run(self._build_from_filters(site=site))

    async def _build_from_filters(self, *, site: str | None = None) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        conn = sqlite3.connect(str(cfg.resolved_db_path()), check_same_thread=False)
        storage = SqliteStorage(conn)
        storage.init_schema()
        try:
            selenium_dispatcher = (
                self.runtime_manager.current_selenium_dispatcher()
                if self.runtime_manager is not None and hasattr(self.runtime_manager, "current_selenium_dispatcher")
                else None
            )
            tracker = WatchlistTracker(
                cfg=cfg,
                storage=storage,
                notifier=TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id),
                selenium_dispatcher=selenium_dispatcher,
            )
            timeout = aiohttp.ClientTimeout(total=max(20, cfg.watchlist_request_timeout_seconds() * 3))
            async with aiohttp.ClientSession(timeout=timeout) as raw_session:
                session = ProxyAwareSession(raw_session, cfg)
                return await tracker.build_from_filters(session, site=site)
        finally:
            conn.close()

    def scan_now(self, *, site: str | None = None, product_key: str | None = None, item_id: int | None = None) -> dict[str, Any]:
        return asyncio.run(self._scan_now(site=site, product_key=product_key, item_id=item_id))

    async def _scan_now(self, *, site: str | None, product_key: str | None, item_id: int | None) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        conn = sqlite3.connect(str(cfg.resolved_db_path()), check_same_thread=False)
        storage = SqliteStorage(conn)
        storage.init_schema()
        try:
            selenium_dispatcher = (
                self.runtime_manager.current_selenium_dispatcher()
                if self.runtime_manager is not None and hasattr(self.runtime_manager, "current_selenium_dispatcher")
                else None
            )
            tracker = WatchlistTracker(
                cfg=cfg,
                storage=storage,
                notifier=TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id),
                selenium_dispatcher=selenium_dispatcher,
            )
            timeout = aiohttp.ClientTimeout(total=max(20, cfg.watchlist_request_timeout_seconds() * 3))
            async with aiohttp.ClientSession(timeout=timeout) as raw_session:
                session = ProxyAwareSession(raw_session, cfg)
                return await tracker.scan_once(session, site=site, product_key=product_key, item_id=item_id)
        finally:
            conn.close()
