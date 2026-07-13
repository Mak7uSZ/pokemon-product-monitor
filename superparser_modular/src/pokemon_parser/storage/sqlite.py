from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading

from pokemon_parser.filters.models import FilterRule
from pokemon_parser.models import Event, ParsedItem, WatchlistProduct
from pokemon_parser.storage.migrations import (
    LATEST_SCHEMA_VERSION,
    SCHEMA_MIGRATION_LOCK,
    apply_schema_migrations,
    migration_process_lock,
    prepare_migration_backup,
)
from pokemon_parser.utils.time import utc_now_iso


logger = logging.getLogger(__name__)


class SqliteStorage:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.RLock()
        self.last_migration_backup_path: str | None = None

    def init_schema(self) -> None:
        with self._lock, SCHEMA_MIGRATION_LOCK, migration_process_lock(self.conn), self.conn:
            current_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            logger.info(
                "sqlite schema initialization started current_version=%s target_version=%s",
                current_version,
                LATEST_SCHEMA_VERSION,
            )
            migration_backup = prepare_migration_backup(self.conn)
            self.last_migration_backup_path = str(migration_backup) if migration_backup else None
            if migration_backup is not None:
                logger.info(
                    "pre-migration backup verified destination=%s/%s",
                    migration_backup.parent.name,
                    migration_backup.name,
                )
            cur = self.conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("BEGIN IMMEDIATE")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    site TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT,
                    title_norm TEXT,
                    url TEXT,
                    price_value REAL,
                    availability_text TEXT,
                    is_available INTEGER DEFAULT 0,
                    seller TEXT,
                    extra_json TEXT,
                    first_seen TEXT,
                    last_seen TEXT,
                    active INTEGER DEFAULT 1,
                    canonical_id TEXT,
                    lifecycle_state TEXT NOT NULL DEFAULT 'discovered',
                    last_checked TEXT,
                    last_successfully_parsed TEXT,
                    last_state_change TEXT,
                    last_error TEXT,
                    missing_count INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    archived_at TEXT,
                    PRIMARY KEY (site, external_id)
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS filters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    sites_json TEXT,
                    keyword_groups_json TEXT,
                    exclude_words_json TEXT,
                    min_price REAL,
                    max_price REAL,
                    soft_price INTEGER DEFAULT 1,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site TEXT,
                    external_id TEXT,
                    event_type TEXT,
                    old_value TEXT,
                    new_value TEXT,
                    matched_filter_ids_json TEXT,
                    created_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS action_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site TEXT,
                    external_id TEXT,
                    action_type TEXT,
                    action_case TEXT,
                    status TEXT,
                    details TEXT,
                    created_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS site_state (
                    site TEXT PRIMARY KEY,
                    parser_cooldown_until REAL DEFAULT 0,
                    worker_cooldown_until REAL DEFAULT 0,
                    parser_denies INTEGER DEFAULT 0,
                    worker_denies INTEGER DEFAULT 0,
                    updated_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    site TEXT,
                    message TEXT NOT NULL,
                    details_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_logs_level
                ON runtime_logs (level)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_logs_category_id
                ON runtime_logs (category, id DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_logs_site_id
                ON runtime_logs (site, id DESC)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS purchase_state (
                    site TEXT NOT NULL,
                    purchase_key TEXT NOT NULL,
                    external_id TEXT,
                    title TEXT,
                    product_url TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    confirmation_url TEXT,
                    confirmation_signal TEXT,
                    error_message TEXT,
                    details_json TEXT,
                    PRIMARY KEY (site, purchase_key)
                )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_purchase_state_status
                ON purchase_state (site, status, updated_at)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS priority_watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    article_number TEXT,
                    sku TEXT,
                    handle TEXT,
                    title TEXT,
                    url TEXT,
                    image_url TEXT,
                    price_value REAL,
                    currency TEXT DEFAULT 'EUR',
                    current_inventory_status TEXT DEFAULT 'unknown',
                    status_confidence_score REAL DEFAULT 0,
                    matched_filter_ids_json TEXT,
                    matched_filter_names_json TEXT,
                    pinned INTEGER DEFAULT 0,
                    enabled INTEGER DEFAULT 1,
                    orphaned INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'auto_filter_match',
                    last_seen_at TEXT,
                    last_checked_at TEXT,
                    last_available_at TEXT,
                    last_status_change_at TEXT,
                    last_error TEXT,
                    extra_json TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    last_successfully_parsed_at TEXT,
                    lifecycle_state TEXT NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1,
                    archived_at TEXT,
                    UNIQUE(site, product_key)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_priority_watchlist_site_key
                ON priority_watchlist (site, product_key)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_priority_watchlist_site_article
                ON priority_watchlist (site, article_number)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_priority_watchlist_site_sku
                ON priority_watchlist (site, sku)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_priority_watchlist_site_handle
                ON priority_watchlist (site, handle)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_priority_watchlist_enabled
                ON priority_watchlist (enabled)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_priority_watchlist_pinned
                ON priority_watchlist (pinned)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_priority_watchlist_status
                ON priority_watchlist (current_inventory_status)
                """
            )

            if current_version < LATEST_SCHEMA_VERSION:
                logger.info(
                    "sqlite schema migration started schema_from=%s schema_to=%s",
                    current_version,
                    LATEST_SCHEMA_VERSION,
                )
            apply_schema_migrations(self.conn, current_version)
            if current_version < LATEST_SCHEMA_VERSION:
                logger.info(
                    "sqlite schema migration complete schema_from=%s schema_to=%s",
                    current_version,
                    LATEST_SCHEMA_VERSION,
                )
            logger.info("sqlite schema initialization complete schema_version=%s", LATEST_SCHEMA_VERSION)
            self.conn.commit()

    def replace_filters(self, filters: list[FilterRule]) -> None:
        with self._lock:
            now = utc_now_iso()
            cur = self.conn.cursor()
            cur.execute("DELETE FROM filters")
            for rule in filters:
                cur.execute(
                    """
                    INSERT INTO filters (
                        id, name, sites_json, keyword_groups_json, exclude_words_json,
                        min_price, max_price, soft_price, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule.id,
                        rule.name,
                        json.dumps(list(rule.sites)),
                        json.dumps([list(group) for group in rule.include_groups]),
                        json.dumps(list(rule.exclude_words)),
                        rule.min_price,
                        rule.max_price,
                        int(rule.soft_price),
                        int(rule.enabled),
                        now,
                        now,
                    ),
                )
            self.conn.commit()

    @staticmethod
    def _row_to_filter_rule(row) -> FilterRule:
        return FilterRule(
            id=row[0],
            name=row[1],
            sites=tuple(json.loads(row[2] or "[]")),
            include_groups=tuple(tuple(group) for group in json.loads(row[3] or "[]")),
            exclude_words=tuple(json.loads(row[4] or "[]")),
            min_price=row[5],
            max_price=row[6],
            soft_price=bool(row[7]),
            enabled=bool(row[8]),
        )

    def load_filters(self) -> list[FilterRule]:
        with self._lock:
            cur = self.conn.cursor()
            rows = cur.execute(
                """
                SELECT id, name, sites_json, keyword_groups_json, exclude_words_json,
                       min_price, max_price, soft_price, enabled
                FROM filters
                WHERE enabled = 1
                ORDER BY id ASC
                """
            ).fetchall()

        return [self._row_to_filter_rule(row) for row in rows]

    def list_filters_all(self) -> list[FilterRule]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, name, sites_json, keyword_groups_json, exclude_words_json,
                       min_price, max_price, soft_price, enabled
                FROM filters
                ORDER BY id ASC
                """
            ).fetchall()
        return [self._row_to_filter_rule(row) for row in rows]

    def get_filter(self, filter_id: int) -> FilterRule | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT id, name, sites_json, keyword_groups_json, exclude_words_json,
                       min_price, max_price, soft_price, enabled
                FROM filters
                WHERE id = ?
                """,
                (int(filter_id),),
            ).fetchone()
        return self._row_to_filter_rule(row) if row is not None else None

    def create_filter(self, rule: FilterRule) -> FilterRule:
        with self._lock:
            now = utc_now_iso()
            cursor = self.conn.execute(
                """
                INSERT INTO filters (
                    name, sites_json, keyword_groups_json, exclude_words_json,
                    min_price, max_price, soft_price, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.name,
                    json.dumps(list(rule.sites)),
                    json.dumps([list(group) for group in rule.include_groups]),
                    json.dumps(list(rule.exclude_words)),
                    rule.min_price,
                    rule.max_price,
                    int(rule.soft_price),
                    int(rule.enabled),
                    now,
                    now,
                ),
            )
            self.conn.commit()
            created_id = int(cursor.lastrowid)

        created = self.get_filter(created_id)
        if created is None:
            raise RuntimeError(f"failed to load created filter id={created_id}")
        return created

    def update_filter(self, rule: FilterRule) -> FilterRule | None:
        with self._lock:
            now = utc_now_iso()
            self.conn.execute(
                """
                UPDATE filters
                SET name = ?, sites_json = ?, keyword_groups_json = ?, exclude_words_json = ?,
                    min_price = ?, max_price = ?, soft_price = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    rule.name,
                    json.dumps(list(rule.sites)),
                    json.dumps([list(group) for group in rule.include_groups]),
                    json.dumps(list(rule.exclude_words)),
                    rule.min_price,
                    rule.max_price,
                    int(rule.soft_price),
                    int(rule.enabled),
                    now,
                    int(rule.id),
                ),
            )
            self.conn.commit()
        return self.get_filter(rule.id)

    def delete_filter(self, filter_id: int) -> bool:
        with self._lock:
            cursor = self.conn.execute("DELETE FROM filters WHERE id = ?", (int(filter_id),))
            self.conn.commit()
            return cursor.rowcount > 0

    def toggle_filter(self, filter_id: int) -> FilterRule | None:
        current = self.get_filter(filter_id)
        if current is None:
            return None
        updated = FilterRule(
            id=current.id,
            name=current.name,
            sites=current.sites,
            include_groups=current.include_groups,
            exclude_words=current.exclude_words,
            min_price=current.min_price,
            max_price=current.max_price,
            soft_price=current.soft_price,
            enabled=not current.enabled,
        )
        return self.update_filter(updated)

    def insert_event(self, event: Event) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO events (site, external_id, event_type, old_value, new_value, matched_filter_ids_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.site,
                    event.external_id,
                    event.event_type,
                    event.old_value,
                    event.new_value,
                    json.dumps(list(event.matched_filter_ids)),
                    utc_now_iso(),
                ),
            )
            self.conn.commit()

    def insert_action_log(
        self,
        site: str,
        external_id: str,
        action_type: str,
        action_case: str,
        status: str,
        details: str,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO action_log (site, external_id, action_type, action_case, status, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (site, external_id, action_type, action_case, status, details, utc_now_iso()),
            )
            self.conn.commit()

    def insert_runtime_log(
        self,
        *,
        level: str,
        category: str,
        message: str,
        site: str | None = None,
        details: dict | str | None = None,
    ) -> None:
        details_json: str | None = None
        if details is not None:
            details_json = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False)

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO runtime_logs (level, category, site, message, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(level).upper(),
                    str(category).lower(),
                    site,
                    message,
                    details_json,
                    utc_now_iso(),
                ),
            )
            self.conn.commit()

    @staticmethod
    def _decode_json(value: str | None, default):
        if not value:
            return default
        try:
            return json.loads(value)
        except Exception:
            return default

    @staticmethod
    def _extract_article_number(value: str | None) -> str | None:
        if not value:
            return None
        text = str(value).strip()
        if text.isdigit() and len(text) >= 5:
            return text
        match = re.search(r"(\d{5,})(?:\.html)?(?:$|[?#])", text)
        return match.group(1) if match else None

    @classmethod
    def _watchlist_product_key(cls, item: ParsedItem) -> str:
        extra = dict(item.extra or {})
        if item.site == "mediamarkt":
            return (
                str(extra.get("article_number") or "")
                or cls._extract_article_number(item.external_id)
                or cls._extract_article_number(item.url)
                or item.external_id
            )
        if item.site == "pocketgames":
            return str(extra.get("handle") or item.external_id or item.url)
        if item.site == "bol":
            return str(extra.get("product_id") or item.external_id or item.url)
        if item.site == "dreamland":
            return str(extra.get("sku") or item.external_id or item.url)
        return str(item.external_id or item.url)

    @staticmethod
    def _row_to_watchlist(row) -> dict | None:
        if row is None:
            return None
        return {
            "id": row[0],
            "site": row[1],
            "product_key": row[2],
            "article_number": row[3],
            "sku": row[4],
            "handle": row[5],
            "title": row[6],
            "url": row[7],
            "image_url": row[8],
            "price_value": row[9],
            "currency": row[10],
            "current_inventory_status": row[11],
            "status_confidence_score": row[12],
            "matched_filter_ids": SqliteStorage._decode_json(row[13], []),
            "matched_filter_names": SqliteStorage._decode_json(row[14], []),
            "pinned": bool(row[15]),
            "enabled": bool(row[16]),
            "orphaned": bool(row[17]),
            "source": row[18],
            "last_seen_at": row[19],
            "last_checked_at": row[20],
            "last_available_at": row[21],
            "last_status_change_at": row[22],
            "last_error": row[23],
            "extra": SqliteStorage._decode_json(row[24], {}),
            "created_at": row[25],
            "updated_at": row[26],
            "last_successfully_parsed_at": row[27],
            "lifecycle_state": row[28],
            "version": int(row[29] or 1),
            "archived_at": row[30],
        }

    @staticmethod
    def _watchlist_select_sql() -> str:
        return """
            SELECT id, site, product_key, article_number, sku, handle, title, url,
                   image_url, price_value, currency, current_inventory_status,
                   status_confidence_score, matched_filter_ids_json,
                   matched_filter_names_json, pinned, enabled, orphaned, source,
                   last_seen_at, last_checked_at, last_available_at,
                   last_status_change_at, last_error, extra_json, created_at, updated_at,
                   last_successfully_parsed_at, lifecycle_state, version, archived_at
            FROM priority_watchlist
        """

    def get_watchlist_item(self, item_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                self._watchlist_select_sql() + " WHERE id = ?",
                (int(item_id),),
            ).fetchone()
        return self._row_to_watchlist(row)

    def list_watchlist(
        self,
        *,
        site: str | None = None,
        enabled: bool | None = None,
        include_archived: bool = False,
        limit: int = 500,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if site:
            clauses.append("site = ?")
            params.append(site)
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(int(enabled))
        if not include_archived:
            clauses.append("archived_at IS NULL")

        sql = self._watchlist_select_sql()
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY pinned DESC, updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(2000, int(limit))))

        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [item for item in (self._row_to_watchlist(row) for row in rows) if item is not None]

    def upsert_watchlist_entry(self, product: WatchlistProduct) -> dict:
        now = utc_now_iso()
        status = product.current_inventory_status or "unknown"
        with self._lock:
            existing = self.conn.execute(
                """
                SELECT id, current_inventory_status, pinned, enabled, source
                FROM priority_watchlist
                WHERE site = ? AND product_key = ?
                """,
                (product.site, product.product_key),
            ).fetchone()
            status_changed_at = now
            current = self.get_watchlist_item(int(existing[0])) if existing is not None else None
            if existing is not None and existing[1] == status:
                status_changed_at = current.get("last_status_change_at") if current else None

            pinned = product.pinned
            enabled = product.enabled
            source = product.source
            if existing is not None:
                pinned = bool(existing[2]) or product.pinned
                enabled = bool(existing[3])
                if existing[4] in {"manual", "imported"}:
                    source = existing[4]

            last_available_at = product.last_available_at
            if status in {"in_stock", "add_to_cart_available", "delivery_available", "offer_available", "variant_available"}:
                last_available_at = now
            lifecycle_state = "active" if last_available_at == now else "unavailable"
            merged_extra = dict(current.get("extra") or {}) if current else {}
            merged_extra.update(dict(product.extra or {}))

            self.conn.execute(
                """
                INSERT INTO priority_watchlist (
                    site, product_key, article_number, sku, handle, title, url,
                    image_url, price_value, currency, current_inventory_status,
                    status_confidence_score, matched_filter_ids_json,
                    matched_filter_names_json, pinned, enabled, orphaned, source,
                    last_seen_at, last_checked_at, last_available_at,
                    last_status_change_at, last_error, extra_json, created_at, updated_at,
                    last_successfully_parsed_at, lifecycle_state, version, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
                ON CONFLICT(site, product_key) DO UPDATE SET
                    article_number = COALESCE(excluded.article_number, article_number),
                    sku = COALESCE(excluded.sku, sku),
                    handle = COALESCE(excluded.handle, handle),
                    title = COALESCE(excluded.title, title),
                    url = COALESCE(excluded.url, url),
                    image_url = COALESCE(excluded.image_url, image_url),
                    price_value = COALESCE(excluded.price_value, price_value),
                    currency = COALESCE(excluded.currency, currency),
                    current_inventory_status = excluded.current_inventory_status,
                    status_confidence_score = excluded.status_confidence_score,
                    matched_filter_ids_json = excluded.matched_filter_ids_json,
                    matched_filter_names_json = excluded.matched_filter_names_json,
                    pinned = excluded.pinned,
                    enabled = excluded.enabled,
                    orphaned = excluded.orphaned,
                    source = excluded.source,
                    last_seen_at = COALESCE(excluded.last_seen_at, last_seen_at),
                    last_checked_at = COALESCE(excluded.last_checked_at, last_checked_at),
                    last_available_at = COALESCE(excluded.last_available_at, last_available_at),
                    last_status_change_at = COALESCE(excluded.last_status_change_at, last_status_change_at),
                    last_error = excluded.last_error,
                    extra_json = excluded.extra_json,
                    updated_at = excluded.updated_at,
                    last_successfully_parsed_at = COALESCE(excluded.last_successfully_parsed_at, last_successfully_parsed_at),
                    lifecycle_state = CASE WHEN archived_at IS NULL THEN excluded.lifecycle_state ELSE lifecycle_state END,
                    version = version + 1
                """,
                (
                    product.site,
                    product.product_key,
                    product.article_number,
                    product.sku,
                    product.handle,
                    product.title,
                    product.url,
                    product.image_url,
                    product.price_value,
                    product.currency,
                    status,
                    float(product.status_confidence_score),
                    json.dumps(list(product.matched_filter_ids)),
                    json.dumps(list(product.matched_filter_names), ensure_ascii=False),
                    int(pinned),
                    int(enabled),
                    int(product.orphaned),
                    source,
                    product.last_seen_at or now,
                    product.last_checked_at,
                    last_available_at,
                    status_changed_at,
                    product.last_error,
                    json.dumps(merged_extra, ensure_ascii=False),
                    now,
                    now,
                    now if product.last_error is None else None,
                    lifecycle_state,
                ),
            )
            self.conn.commit()
            row = self.conn.execute(
                self._watchlist_select_sql() + " WHERE site = ? AND product_key = ?",
                (product.site, product.product_key),
            ).fetchone()
        loaded = self._row_to_watchlist(row)
        if loaded is None:
            raise RuntimeError("failed to load upserted watchlist item")
        return loaded

    def upsert_watchlist_from_item(
        self,
        item: ParsedItem,
        matched_rules: list[FilterRule],
        *,
        source: str = "auto_filter_match",
    ) -> dict:
        extra = dict(item.extra or {})
        product_key = self._watchlist_product_key(item)
        article_number = extra.get("article_number") or self._extract_article_number(item.external_id) or self._extract_article_number(item.url)
        status = str(extra.get("availability_status") or ("in_stock" if item.is_available else "out_of_stock"))
        confidence = extra.get("status_confidence_score", extra.get("availability_confidence_score"))
        if confidence is None:
            confidence = 1.0 if item.is_available else 0.5
        product = WatchlistProduct(
            site=item.site,
            product_key=str(product_key),
            article_number=str(article_number) if article_number else None,
            sku=str(extra.get("sku")) if extra.get("sku") else None,
            handle=str(extra.get("handle")) if extra.get("handle") else None,
            title=item.title,
            url=item.url,
            image_url=extra.get("image_url"),
            price_value=item.price_value,
            current_inventory_status=status,
            status_confidence_score=float(confidence),
            matched_filter_ids=tuple(rule.id for rule in matched_rules),
            matched_filter_names=tuple(rule.name for rule in matched_rules),
            source=source,
            last_seen_at=utc_now_iso(),
            extra={
                "auto_watchlist_reason": source,
                "availability_text": item.availability_text,
                "purchasable": extra.get("purchasable", item.is_available),
                "parser_extra": extra,
            },
        )
        return self.upsert_watchlist_entry(product)

    def update_watchlist_item(self, item_id: int, values: dict) -> dict | None:
        allowed = {
            "title",
            "url",
            "image_url",
            "price_value",
            "current_inventory_status",
            "status_confidence_score",
            "pinned",
            "enabled",
            "orphaned",
            "source",
            "last_error",
        }
        assignments: list[str] = []
        params: list[object] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            column_value = int(value) if key in {"pinned", "enabled", "orphaned"} else value
            assignments.append(f"{key} = ?")
            params.append(column_value)
        if not assignments:
            return self.get_watchlist_item(item_id)
        assignments.append("updated_at = ?")
        params.append(utc_now_iso())
        assignments.append("version = version + 1")
        params.append(int(item_id))
        with self._lock:
            self.conn.execute(
                f"UPDATE priority_watchlist SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            self.conn.commit()
        return self.get_watchlist_item(item_id)

    def delete_watchlist_item(self, item_id: int) -> bool:
        now = utc_now_iso()
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE priority_watchlist
                SET lifecycle_state = 'archived', archived_at = ?, enabled = 0,
                    updated_at = ?, version = version + 1
                WHERE id = ? AND archived_at IS NULL
                """,
                (now, now, int(item_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def update_watchlist_check(
        self,
        item_id: int,
        *,
        status: str,
        confidence: float,
        last_error: str | None = None,
        price_value: float | None = None,
        title: str | None = None,
        url: str | None = None,
        image_url: str | None = None,
        extra: dict | None = None,
    ) -> dict | None:
        existing = self.get_watchlist_item(item_id)
        if existing is None:
            return None
        now = utc_now_iso()
        # A parser may attach a diagnostic (for example ``pdp_404``) to a
        # conclusive negative observation.  Preserve the previous good value
        # only for statuses that explicitly mean the observation itself was
        # unsuccessful.
        unsuccessful_statuses = {
            "error",
            "parse_unknown",
            "unknown_error",
            "rate_limited_unknown",
        }
        successful_parse = status not in unsuccessful_statuses
        merged_extra = dict(existing.get("extra") or {})
        if extra is not None:
            merged_extra.update(extra)
        if not successful_parse:
            with self._lock:
                self.conn.execute(
                    """
                    UPDATE priority_watchlist
                    SET last_checked_at = ?, last_error = ?, extra_json = ?,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (
                        now,
                        str(last_error or status)[:1000],
                        json.dumps(merged_extra, ensure_ascii=False),
                        now,
                        int(item_id),
                    ),
                )
                self.conn.commit()
            return self.get_watchlist_item(item_id)
        status_changed_at = existing.get("last_status_change_at")
        if existing.get("current_inventory_status") != status:
            status_changed_at = now
        last_available_at = existing.get("last_available_at")
        if status in {"in_stock", "add_to_cart_available", "delivery_available", "offer_available", "variant_available"}:
            last_available_at = now
        with self._lock:
            self.conn.execute(
                """
                UPDATE priority_watchlist
                SET current_inventory_status = ?,
                    status_confidence_score = ?,
                    title = COALESCE(?, title),
                    url = COALESCE(?, url),
                    image_url = COALESCE(?, image_url),
                    price_value = COALESCE(?, price_value),
                    last_checked_at = ?,
                    last_available_at = ?,
                    last_status_change_at = ?,
                    last_error = ?,
                    extra_json = ?,
                    updated_at = ?,
                    last_successfully_parsed_at = ?,
                    lifecycle_state = ?,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    status,
                    float(confidence),
                    title,
                    url,
                    image_url,
                    price_value,
                    now,
                    last_available_at,
                    status_changed_at,
                    last_error,
                    json.dumps(merged_extra, ensure_ascii=False),
                    now,
                    now,
                    "active" if status in {"in_stock", "add_to_cart_available", "delivery_available", "offer_available", "variant_available"} else "unavailable",
                    int(item_id),
                ),
            )
            self.conn.commit()
        return self.get_watchlist_item(item_id)

    def watchlist_summary(self) -> dict:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT site, current_inventory_status, enabled, orphaned, COUNT(*)
                FROM priority_watchlist
                WHERE archived_at IS NULL
                GROUP BY site, current_inventory_status, enabled, orphaned
                """
            ).fetchall()
        total = 0
        output = {
            "total": 0,
            "enabled": 0,
            "available": 0,
            "out_of_stock": 0,
            "unknown_or_error": 0,
            "orphaned": 0,
            "sites": {},
        }
        available_statuses = {"in_stock", "add_to_cart_available", "delivery_available", "offer_available", "variant_available"}
        out_statuses = {"out_of_stock", "unavailable", "notify_only", "soon_available", "not_found_currently"}
        for site, status, enabled, orphaned, count in rows:
            count = int(count or 0)
            total += count
            site_summary = output["sites"].setdefault(
                site,
                {"total": 0, "enabled": 0, "available": 0, "out_of_stock": 0, "unknown_or_error": 0, "orphaned": 0},
            )
            site_summary["total"] += count
            if enabled:
                output["enabled"] += count
                site_summary["enabled"] += count
            if orphaned:
                output["orphaned"] += count
                site_summary["orphaned"] += count
            if status in available_statuses:
                output["available"] += count
                site_summary["available"] += count
            elif status in out_statuses:
                output["out_of_stock"] += count
                site_summary["out_of_stock"] += count
            else:
                output["unknown_or_error"] += count
                site_summary["unknown_or_error"] += count
        output["total"] = total
        return output

    def watchlist_state(self, *, limit: int = 2000) -> dict:
        """Return items and aggregate counts from one SQLite read snapshot."""
        with self._lock:
            self.conn.execute("SAVEPOINT watchlist_state")
            try:
                items = self.list_watchlist(limit=limit)
                summary = self.watchlist_summary()
                revision_row = self.conn.execute(
                    """
                    SELECT COALESCE(MAX(updated_at), ''), COALESCE(SUM(version), 0), COUNT(*)
                    FROM priority_watchlist
                    WHERE archived_at IS NULL
                    """
                ).fetchone()
                self.conn.execute("RELEASE SAVEPOINT watchlist_state")
            except Exception:
                self.conn.execute("ROLLBACK TO SAVEPOINT watchlist_state")
                self.conn.execute("RELEASE SAVEPOINT watchlist_state")
                raise
        return {
            "items": items,
            "summary": summary,
            "revision": f"{revision_row[0]}:{int(revision_row[1])}:{int(revision_row[2])}",
        }

    def watchlist_site_diagnostics(self) -> dict[str, dict]:
        diagnostics: dict[str, dict] = {}
        with self._lock:
            site_rows = self.conn.execute(
                """
                SELECT site,
                       SUM(CASE WHEN current_inventory_status = 'parse_unknown' THEN 1 ELSE 0 END) AS parse_unknown_count,
                       SUM(CASE WHEN current_inventory_status IN ('rate_limited_unknown', 'http_429_throttled') THEN 1 ELSE 0 END) AS rate_limited_count
                FROM priority_watchlist
                GROUP BY site
                """
            ).fetchall()
            latest_rows = self.conn.execute(
                """
                SELECT site, product_key, article_number, current_inventory_status,
                       status_confidence_score, last_checked_at, last_error, extra_json
                FROM priority_watchlist
                WHERE last_checked_at IS NOT NULL
                ORDER BY last_checked_at DESC, updated_at DESC, id DESC
                """
            ).fetchall()

        for row in site_rows:
            diagnostics[str(row[0])] = {
                "parse_unknown_count": int(row[1] or 0),
                "rate_limited_count": int(row[2] or 0),
            }

        seen_latest: set[str] = set()
        for row in latest_rows:
            site = str(row[0])
            if site in seen_latest:
                continue
            seen_latest.add(site)
            payload = diagnostics.setdefault(site, {"parse_unknown_count": 0, "rate_limited_count": 0})
            extra = self._decode_json(row[7], {})
            result_extra = extra.get("result_extra") if isinstance(extra, dict) else {}
            if not isinstance(result_extra, dict):
                result_extra = {}
            diagnostic = extra.get("pdp_diagnostic") if isinstance(extra, dict) else None
            if not isinstance(diagnostic, dict):
                diagnostic = result_extra.get("pdp_diagnostic") if isinstance(result_extra, dict) else {}
            if not isinstance(diagnostic, dict):
                diagnostic = {}

            buyable_marker_found = bool(
                extra.get("buyable_marker_found")
                or result_extra.get("buyable_marker_found")
                or diagnostic.get("add_to_cart_button_found")
                or diagnostic.get("delivery_available_marker")
                or diagnostic.get("online_status_available_marker")
            )
            alert_notify_marker_found = bool(
                extra.get("alert_notify_marker_found")
                or result_extra.get("alert_notify_marker_found")
                or diagnostic.get("alert_button_found")
                or diagnostic.get("notify_text_found")
                or diagnostic.get("soon_available_text_found")
            )
            action_target_exists = bool(extra.get("action_target_exists") or diagnostic.get("action_target_exists"))

            payload.update(
                {
                    "last_product_key": row[1],
                    "last_article_number": row[2],
                    "last_status": row[3],
                    "last_confidence": row[4],
                    "last_checked_at": row[5],
                    "last_error": row[6],
                    "last_endpoint": extra.get("source_endpoint") if isinstance(extra, dict) else None,
                    "last_http_status": extra.get("http_status") if isinstance(extra, dict) else None,
                    "last_action_target_exists": action_target_exists,
                    "last_skip_reason": extra.get("skip_reason") if isinstance(extra, dict) else None,
                    "buyable_marker_found": buyable_marker_found,
                    "alert_notify_marker_found": alert_notify_marker_found,
                    "last_diagnostic_summary": {
                        key: diagnostic.get(key)
                        for key in (
                            "delivery_available_marker",
                            "delivery_not_available_marker",
                            "online_status_available_marker",
                            "add_to_cart_button_found",
                            "add_to_cart_button_disabled",
                            "alert_button_found",
                            "soon_available_text_found",
                            "notify_text_found",
                            "pickup_selector_found",
                            "final_status",
                            "confidence",
                            "action_target_exists",
                            "conflicting_signals",
                        )
                    },
                }
            )
        return diagnostics

    def product_state_map(self, site: str, external_ids: list[str]) -> dict[str, dict]:
        ids = [external_id for external_id in dict.fromkeys(external_ids) if external_id]
        if not ids:
            return {}

        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT external_id, is_available, availability_text, price_value, active
                FROM products
                WHERE site = ? AND external_id IN ({placeholders})
                """,
                [site, *ids],
            ).fetchall()

        return {
            row[0]: {
                "is_available": bool(row[1]),
                "availability_text": row[2],
                "price_value": row[3],
                "active": bool(row[4]),
            }
            for row in rows
        }

    @staticmethod
    def _purchase_state_from_row(row) -> dict | None:
        if row is None:
            return None
        return {
            "site": row[0],
            "purchase_key": row[1],
            "external_id": row[2],
            "title": row[3],
            "product_url": row[4],
            "status": row[5],
            "created_at": row[6],
            "updated_at": row[7],
            "last_attempt_at": row[8],
            "confirmation_url": row[9],
            "confirmation_signal": row[10],
            "error_message": row[11],
            "details_json": row[12],
        }

    def get_purchase_state(self, site: str, purchase_key: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT site, purchase_key, external_id, title, product_url, status,
                       created_at, updated_at, last_attempt_at, confirmation_url,
                       confirmation_signal, error_message, details_json
                FROM purchase_state
                WHERE site = ? AND purchase_key = ?
                """,
                (site, purchase_key),
            ).fetchone()
        return self._purchase_state_from_row(row)

    def reserve_purchase_state(
        self,
        *,
        site: str,
        purchase_key: str,
        external_id: str,
        title: str,
        product_url: str,
        blocking_statuses: set[str],
        status: str = "queued",
        details: dict | None = None,
    ) -> tuple[bool, dict | None]:
        with self._lock:
            existing = self.get_purchase_state(site, purchase_key)
            if existing is not None and existing["status"] in blocking_statuses:
                return False, existing

            now = utc_now_iso()
            details_json = json.dumps(details or {}, ensure_ascii=False)
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO purchase_state (
                        site, purchase_key, external_id, title, product_url, status,
                        created_at, updated_at, last_attempt_at, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        site,
                        purchase_key,
                        external_id,
                        title,
                        product_url,
                        status,
                        now,
                        now,
                        now,
                        details_json,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE purchase_state
                    SET external_id = ?, title = ?, product_url = ?, status = ?,
                        updated_at = ?, last_attempt_at = ?, confirmation_url = NULL,
                        confirmation_signal = NULL, error_message = NULL, details_json = ?
                    WHERE site = ? AND purchase_key = ?
                    """,
                    (
                        external_id,
                        title,
                        product_url,
                        status,
                        now,
                        now,
                        details_json,
                        site,
                        purchase_key,
                    ),
                )
            self.conn.commit()
            return True, self.get_purchase_state(site, purchase_key)

    def update_purchase_state(
        self,
        *,
        site: str,
        purchase_key: str,
        status: str,
        external_id: str | None = None,
        title: str | None = None,
        product_url: str | None = None,
        confirmation_url: str | None = None,
        confirmation_signal: str | None = None,
        error_message: str | None = None,
        details: dict | None = None,
    ) -> dict | None:
        with self._lock:
            existing = self.get_purchase_state(site, purchase_key)
            now = utc_now_iso()
            details_json = json.dumps(details or {}, ensure_ascii=False) if details is not None else None

            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO purchase_state (
                        site, purchase_key, external_id, title, product_url, status,
                        created_at, updated_at, last_attempt_at, confirmation_url,
                        confirmation_signal, error_message, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        site,
                        purchase_key,
                        external_id,
                        title,
                        product_url,
                        status,
                        now,
                        now,
                        now,
                        confirmation_url,
                        confirmation_signal,
                        error_message,
                        details_json,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE purchase_state
                    SET external_id = COALESCE(?, external_id),
                        title = COALESCE(?, title),
                        product_url = COALESCE(?, product_url),
                        status = ?,
                        updated_at = ?,
                        last_attempt_at = CASE
                            WHEN ? IN ('queued', 'running', 'payment_submitted') THEN ?
                            ELSE last_attempt_at
                        END,
                        confirmation_url = COALESCE(?, confirmation_url),
                        confirmation_signal = COALESCE(?, confirmation_signal),
                        error_message = ?,
                        details_json = COALESCE(?, details_json)
                    WHERE site = ? AND purchase_key = ?
                    """,
                    (
                        external_id,
                        title,
                        product_url,
                        status,
                        now,
                        status,
                        now,
                        confirmation_url,
                        confirmation_signal,
                        error_message,
                        details_json,
                        site,
                        purchase_key,
                    ),
                )
            self.conn.commit()
            return self.get_purchase_state(site, purchase_key)

    def list_runtime_logs(
        self,
        *,
        log_type: str | None = None,
        site: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []

        normalized_type = (log_type or "").strip().lower()
        if normalized_type == "error":
            clauses.append("(category = ? OR level IN ('ERROR', 'CRITICAL'))")
            params.append("error")
        elif normalized_type in {"heartbeat", "success", "worker_trace", "action", "scan", "runtime"}:
            clauses.append("category = ?")
            params.append(normalized_type)

        if site:
            clauses.append("site = ?")
            params.append(site)

        if query:
            clauses.append("(message LIKE ? OR COALESCE(details_json, '') LIKE ?)")
            needle = f"%{query}%"
            params.extend([needle, needle])

        sql = """
            SELECT id, level, category, site, message, details_json, created_at
            FROM runtime_logs
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))

        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()

        return [
            {
                "id": row[0],
                "level": row[1],
                "category": row[2],
                "site": row[3],
                "message": row[4],
                "details_json": row[5],
                "timestamp": row[6],
            }
            for row in rows
        ]

    def runtime_log_summary(self, *, tail_limit: int = 50) -> dict:
        with self._lock:
            level_rows = self.conn.execute(
                """
                SELECT level, COUNT(*)
                FROM runtime_logs
                GROUP BY level
                """
            ).fetchall()
            tail_rows = self.conn.execute(
                """
                SELECT level, category, site, message, created_at
                FROM runtime_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(tail_limit)),),
            ).fetchall()

        counts = {
            "info": 0,
            "warning": 0,
            "error": 0,
        }
        for level, count in level_rows:
            level = str(level).lower()
            if level in counts:
                counts[level] = count

        tail = [
            {
                "level": row[0],
                "category": row[1],
                "site": row[2],
                "message": row[3],
                "timestamp": row[4],
            }
            for row in reversed(tail_rows)
        ]
        return {
            "counts": counts,
            "tail": tail,
        }

    def site_product_stats(self) -> dict[str, dict]:
        with self._lock:
            product_rows = self.conn.execute(
                """
                SELECT
                    site,
                    COUNT(*) AS product_count,
                    SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_product_count,
                    SUM(CASE WHEN is_available = 1 THEN 1 ELSE 0 END) AS in_stock_count,
                    SUM(CASE WHEN is_available = 0 THEN 1 ELSE 0 END) AS out_of_stock_count,
                    MAX(last_seen) AS last_seen
                FROM products
                GROUP BY site
                """
            ).fetchall()
            event_rows = self.conn.execute(
                """
                SELECT site, COUNT(*)
                FROM events
                GROUP BY site
                """
            ).fetchall()

        output: dict[str, dict] = {}
        for site, product_count, active_product_count, in_stock_count, out_of_stock_count, last_seen in product_rows:
            output[site] = {
                "product_count": int(product_count or 0),
                "active_product_count": int(active_product_count or 0),
                "in_stock_count": int(in_stock_count or 0),
                "out_of_stock_count": int(out_of_stock_count or 0),
                "event_count": 0,
                "last_seen": last_seen,
            }
        for site, event_count in event_rows:
            output.setdefault(
                site,
                {
                    "product_count": 0,
                    "active_product_count": 0,
                    "in_stock_count": 0,
                    "out_of_stock_count": 0,
                    "event_count": 0,
                    "last_seen": None,
                },
            )
            output[site]["event_count"] = int(event_count or 0)
        return output

    def was_already_queued(self, site: str, external_id: str, within_minutes: int = 10) -> bool:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT created_at
                FROM action_log
                WHERE site = ? AND external_id = ? AND action_type = 'selenium_queue'
                ORDER BY id DESC
                LIMIT 1
                """,
                (site, external_id),
            ).fetchone()

        if row is None or not row[0]:
            return False

        try:
            from datetime import datetime, timezone

            created_at = row[0].strip()
            if created_at.endswith("Z"):
                created_at = created_at[:-1] + "+00:00"

            queued_dt = datetime.fromisoformat(created_at)
            if queued_dt.tzinfo is None:
                queued_dt = queued_dt.replace(tzinfo=timezone.utc)

            now_dt = datetime.now(timezone.utc)
            age_seconds = (now_dt - queued_dt).total_seconds()
            return age_seconds <= max(0, within_minutes) * 60
        except Exception:
            return False

    def list_products(
        self,
        *,
        site: str | None = None,
        lifecycle_state: str | None = None,
        include_archived: bool = False,
        limit: int = 1000,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if site:
            clauses.append("site = ?")
            params.append(str(site).strip().lower())
        if lifecycle_state:
            clauses.append("lifecycle_state = ?")
            params.append(str(lifecycle_state).strip().lower())
        if not include_archived:
            clauses.append("archived_at IS NULL")
        sql = """
            SELECT site, external_id, canonical_id, title, url, price_value,
                   availability_text, is_available, seller, extra_json,
                   first_seen, last_seen, active, lifecycle_state, last_checked,
                   last_successfully_parsed, last_state_change, last_error,
                   missing_count, version, archived_at
            FROM products
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(last_seen, first_seen) DESC, site, external_id LIMIT ?"
        params.append(max(1, min(5000, int(limit))))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "site": row[0],
                "external_id": row[1],
                "canonical_id": row[2] or row[1],
                "title": row[3],
                "url": row[4],
                "price_value": row[5],
                "availability_text": row[6],
                "is_available": bool(row[7]),
                "seller": row[8],
                "extra": self._decode_json(row[9], {}),
                "first_seen": row[10],
                "last_seen": row[11],
                "active": bool(row[12]),
                "lifecycle_state": row[13],
                "last_checked": row[14],
                "last_successfully_parsed": row[15],
                "last_state_change": row[16],
                "last_error": row[17],
                "missing_count": int(row[18] or 0),
                "version": int(row[19] or 1),
                "archived_at": row[20],
            }
            for row in rows
        ]

    def archive_product(self, site: str, external_id: str) -> bool:
        now = utc_now_iso()
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE products
                SET lifecycle_state = 'archived', archived_at = ?,
                    last_state_change = ?, version = version + 1
                WHERE site = ? AND external_id = ? AND archived_at IS NULL
                """,
                (now, now, str(site).strip().lower(), str(external_id)),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def record_product_check_failure(self, site: str, external_id: str, error: str) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE products
                SET last_checked = ?, last_error = ?, version = version + 1
                WHERE site = ? AND external_id = ?
                """,
                (utc_now_iso(), str(error)[:1000], str(site).strip().lower(), str(external_id)),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def upsert_items(self, items: list[ParsedItem], *, reconcile_missing: bool = True) -> list[Event]:
        with self._lock:
            cur = self.conn.cursor()
            seen_by_site: dict[str, set[str]] = {}
            now = utc_now_iso()
            events: list[Event] = []

            for item in items:
                seen_by_site.setdefault(item.site, set()).add(item.external_id)
                canonical_id = str((item.extra or {}).get("product_id") or item.external_id)
                lifecycle_state = "active" if item.is_available else "unavailable"

                existing = cur.execute(
                    """
                    SELECT title, price_value, availability_text, is_available, seller, active,
                           lifecycle_state, last_state_change
                    FROM products
                    WHERE site = ? AND external_id = ?
                    """,
                    (item.site, item.external_id),
                ).fetchone()

                if existing is None:
                    cur.execute(
                        """
                        INSERT INTO products (
                            site, external_id, title, title_norm, url, price_value, availability_text,
                            is_available, seller, extra_json, first_seen, last_seen, active,
                            canonical_id, lifecycle_state, last_checked, last_successfully_parsed,
                            last_state_change, last_error, missing_count, version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, NULL, 0, 1)
                        """,
                        (
                            item.site,
                            item.external_id,
                            item.title,
                            item.title_norm,
                            item.url,
                            item.price_value,
                            item.availability_text,
                            int(item.is_available),
                            item.seller,
                            json.dumps(dict(item.extra)),
                            now,
                            now,
                            canonical_id,
                            lifecycle_state,
                            now,
                            now,
                            now,
                        ),
                    )
                    events.append(
                        Event(
                            site=item.site,
                            external_id=item.external_id,
                            event_type="new_item",
                            new_value=item.title,
                        )
                    )
                    continue

                (
                    _, old_price, old_availability, old_is_available, old_seller,
                    old_active, old_lifecycle_state, old_state_change,
                ) = existing

                if old_price != item.price_value:
                    events.append(
                        Event(
                            site=item.site,
                            external_id=item.external_id,
                            event_type="price_changed",
                            old_value=str(old_price),
                            new_value=str(item.price_value),
                        )
                    )

                if not bool(old_is_available) and item.is_available:
                    events.append(
                        Event(
                            site=item.site,
                            external_id=item.external_id,
                            event_type="restock",
                            old_value=str(old_availability),
                            new_value=str(item.availability_text),
                        )
                    )

                if old_seller != item.seller:
                    events.append(
                        Event(
                            site=item.site,
                            external_id=item.external_id,
                            event_type="seller_changed",
                            old_value=str(old_seller),
                            new_value=str(item.seller),
                        )
                    )

                if not bool(old_active):
                    events.append(
                        Event(
                            site=item.site,
                            external_id=item.external_id,
                            event_type="returned_to_listing",
                            old_value="inactive",
                            new_value="active",
                        )
                    )

                cur.execute(
                    """
                    UPDATE products
                    SET title = ?, title_norm = ?, url = ?, price_value = ?, availability_text = ?,
                        is_available = ?, seller = ?, extra_json = ?, last_seen = ?, active = 1,
                        canonical_id = ?, lifecycle_state = ?, last_checked = ?,
                        last_successfully_parsed = ?, last_state_change = ?, last_error = NULL,
                        missing_count = 0, version = version + 1
                    WHERE site = ? AND external_id = ?
                    """,
                    (
                        item.title,
                        item.title_norm,
                        item.url,
                        item.price_value,
                        item.availability_text,
                        int(item.is_available),
                        item.seller,
                        json.dumps(dict(item.extra)),
                        now,
                        canonical_id,
                        lifecycle_state,
                        now,
                        now,
                        (
                            now
                            if bool(old_is_available) != bool(item.is_available)
                            or not bool(old_active)
                            or old_lifecycle_state != lifecycle_state
                            else old_state_change
                        ),
                        item.site,
                        item.external_id,
                    ),
                )

            for site, seen_ids in (seen_by_site.items() if reconcile_missing else ()):
                active_rows = cur.execute(
                    "SELECT external_id FROM products WHERE site = ? AND active = 1",
                    (site,),
                ).fetchall()
                for (external_id,) in active_rows:
                    if external_id not in seen_ids:
                        cur.execute(
                            """
                            UPDATE products
                            SET active = 0, lifecycle_state = 'unavailable', missing_count = missing_count + 1,
                                last_state_change = ?, version = version + 1
                            WHERE site = ? AND external_id = ?
                            """,
                            (now, site, external_id),
                        )
                        events.append(
                            Event(
                                site=site,
                                external_id=external_id,
                                event_type="disappeared",
                                old_value="active",
                                new_value="inactive",
                            )
                        )

            self.conn.commit()
            return events
