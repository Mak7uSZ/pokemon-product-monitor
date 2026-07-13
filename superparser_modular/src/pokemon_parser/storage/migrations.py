from __future__ import annotations

import json
import errno
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from pokemon_parser.storage.backup import backup_sqlite_connection

LATEST_SCHEMA_VERSION = 3
SCHEMA_MIGRATION_LOCK = threading.RLock()
MIGRATION_LOCK_STALE_SECONDS = 15 * 60

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationProcessLock:
    path: Path | None
    recovered_stale_lock: bool = False
    previous_owner_pid: int | None = None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno == errno.ESRCH or getattr(exc, "winerror", None) in {87, 1168}:
            return False
        return True


def _read_lock_metadata(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _cleanup_abandoned_migration_temporaries(
    database_path: Path,
    *,
    previous_owner_pid: int | None,
) -> list[Path]:
    backup_dir = database_path.parent / "backups"
    if not backup_dir.is_dir():
        return []
    pattern = re.compile(
        rf"^\.{re.escape(database_path.stem)}-pre-migration-v\d+-to-v\d+-"
        rf"\d{{8}}T\d{{6}}Z(?:-\d+)?{re.escape(database_path.suffix)}\."
        rf"[0-9a-f]{{32}}\.tmp(?:-(?:journal|wal|shm))?$",
        flags=re.IGNORECASE,
    )
    removed: list[Path] = []
    for candidate in backup_dir.iterdir():
        if not candidate.is_file() or not pattern.fullmatch(candidate.name):
            continue
        try:
            candidate.unlink()
            removed.append(candidate)
        except OSError:
            logger.warning(
                "migration temporary cleanup skipped file_in_use name=%s previous_owner_pid=%s",
                candidate.name,
                previous_owner_pid,
            )
    if removed:
        logger.warning(
            "removed abandoned migration temporary files count=%s previous_owner_pid=%s names=%s",
            len(removed),
            previous_owner_pid,
            [path.name for path in removed],
        )
    return removed


@contextmanager
def migration_process_lock(conn: sqlite3.Connection) -> Iterator[MigrationProcessLock]:
    """Prevent concurrent cross-process schema backup/migration for a file database."""

    database_path = _database_path(conn)
    if database_path is None:
        yield MigrationProcessLock(path=None)
        return

    lock_path = database_path.with_name(f"{database_path.name}.startup-migration.lock")
    token = uuid4().hex
    recovered = False
    previous_owner_pid: int | None = None
    while True:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            metadata = _read_lock_metadata(lock_path)
            owner_pid = int(metadata.get("pid") or 0)
            created_epoch = float(metadata.get("created_epoch") or 0.0)
            age_seconds = max(0.0, time.time() - created_epoch) if created_epoch else 0.0
            if _pid_is_running(owner_pid):
                raise RuntimeError(
                    f"Another startup/migration process is active for {database_path.name} (pid={owner_pid})."
                )
            if not created_epoch or age_seconds < MIGRATION_LOCK_STALE_SECONDS:
                raise RuntimeError(
                    f"A recent startup/migration lock exists for {database_path.name}; retry after confirming the prior process stopped."
                )
            try:
                lock_path.unlink()
            except FileNotFoundError:
                continue
            recovered = True
            previous_owner_pid = owner_pid or None
            continue

        try:
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "created_epoch": time.time(),
                    "database_name": database_path.name,
                    "token": token,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        break

    try:
        if recovered:
            _cleanup_abandoned_migration_temporaries(
                database_path,
                previous_owner_pid=previous_owner_pid,
            )
        yield MigrationProcessLock(
            path=lock_path,
            recovered_stale_lock=recovered,
            previous_owner_pid=previous_owner_pid,
        )
    finally:
        metadata = _read_lock_metadata(lock_path)
        if metadata.get("token") == token:
            lock_path.unlink(missing_ok=True)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split(None, 1)[0].strip('"')
    if column not in _columns(conn, table):
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {definition}')


def _database_path(conn: sqlite3.Connection) -> Path | None:
    for _sequence, name, path in conn.execute("PRAGMA database_list").fetchall():
        if name == "main" and path:
            return Path(path).resolve()
    return None


def prepare_migration_backup(conn: sqlite3.Connection) -> Path | None:
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version > LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{current_version} is newer than supported v{LATEST_SCHEMA_VERSION}."
        )
    if current_version == LATEST_SCHEMA_VERSION or not _table_names(conn):
        return None

    database_path = _database_path(conn)
    if database_path is None or not database_path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = (
        database_path.parent
        / "backups"
        / f"{database_path.stem}-pre-migration-v{current_version}-to-v{LATEST_SCHEMA_VERSION}-{stamp}{database_path.suffix}"
    )
    if target.exists():
        target = target.with_name(f"{target.stem}-{threading.get_ident()}{target.suffix}")
    logger.warning(
        "pre-migration backup required database=%s schema_from=%s schema_to=%s destination=%s",
        database_path.name,
        current_version,
        LATEST_SCHEMA_VERSION,
        f"{target.parent.name}/{target.name}",
    )
    return backup_sqlite_connection(conn, target)


def _migrate_lifecycle_columns(conn: sqlite3.Connection) -> None:
    if "products" in _table_names(conn):
        for definition in (
            "canonical_id TEXT",
            "lifecycle_state TEXT NOT NULL DEFAULT 'discovered'",
            "last_checked TEXT",
            "last_successfully_parsed TEXT",
            "last_state_change TEXT",
            "last_error TEXT",
            "missing_count INTEGER NOT NULL DEFAULT 0",
            "version INTEGER NOT NULL DEFAULT 1",
            "archived_at TEXT",
        ):
            _add_column(conn, "products", definition)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_site_canonical ON products (site, canonical_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_lifecycle ON products (site, lifecycle_state, last_seen)"
        )

    if "priority_watchlist" in _table_names(conn):
        for definition in (
            "last_successfully_parsed_at TEXT",
            "lifecycle_state TEXT NOT NULL DEFAULT 'active'",
            "version INTEGER NOT NULL DEFAULT 1",
            "archived_at TEXT",
        ):
            _add_column(conn, "priority_watchlist", definition)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_priority_watchlist_lifecycle ON priority_watchlist (lifecycle_state, updated_at)"
        )


def _bol_product_id(external_id: str, url: str, extra_json: str | None) -> str | None:
    try:
        extra = json.loads(extra_json or "{}")
    except (TypeError, ValueError):
        extra = {}
    product_id = str(extra.get("product_id") or "").strip()
    if product_id.isdigit():
        return product_id
    for value in (url, external_id):
        match = re.search(r"(?:/|_)(\d{8,})(?:/|_|$)", str(value or ""))
        if match:
            return match.group(1)
    return None


def _migrate_bol_identity(conn: sqlite3.Connection) -> None:
    if "products" not in _table_names(conn):
        return
    rows = conn.execute(
        """
        SELECT external_id, title, title_norm, url, price_value, availability_text,
               is_available, seller, extra_json, first_seen, last_seen, active,
               lifecycle_state, last_checked, last_successfully_parsed,
               last_state_change, last_error, missing_count, version, archived_at
        FROM products WHERE site = 'bol'
        ORDER BY COALESCE(last_seen, first_seen, '') ASC, external_id ASC
        """
    ).fetchall()
    for row in rows:
        legacy_id = str(row[0])
        product_id = _bol_product_id(legacy_id, str(row[3] or ""), row[8])
        if not product_id:
            conn.execute(
                "UPDATE products SET canonical_id = COALESCE(canonical_id, external_id) WHERE site = 'bol' AND external_id = ?",
                (legacy_id,),
            )
            continue
        existing = conn.execute(
            "SELECT external_id, first_seen, last_seen, active, version FROM products WHERE site = 'bol' AND external_id = ?",
            (product_id,),
        ).fetchone()
        if legacy_id == product_id:
            conn.execute(
                "UPDATE products SET canonical_id = ?, version = MAX(version, 1) WHERE site = 'bol' AND external_id = ?",
                (product_id, legacy_id),
            )
        elif existing is None:
            conn.execute(
                "UPDATE products SET external_id = ?, canonical_id = ?, version = MAX(version, 1) WHERE site = 'bol' AND external_id = ?",
                (product_id, product_id, legacy_id),
            )
        else:
            first_seen = min(value for value in (existing[1], row[9]) if value) if existing[1] or row[9] else None
            last_seen = max(value for value in (existing[2], row[10]) if value) if existing[2] or row[10] else None
            conn.execute(
                """
                UPDATE products
                SET canonical_id = ?, title = COALESCE(?, title), title_norm = COALESCE(?, title_norm),
                    url = COALESCE(?, url), price_value = COALESCE(?, price_value),
                    availability_text = COALESCE(?, availability_text), is_available = ?,
                    seller = COALESCE(?, seller), extra_json = COALESCE(?, extra_json),
                    first_seen = ?, last_seen = ?, active = MAX(active, ?),
                    lifecycle_state = COALESCE(?, lifecycle_state),
                    last_checked = COALESCE(?, last_checked),
                    last_successfully_parsed = COALESCE(?, last_successfully_parsed),
                    last_state_change = COALESCE(?, last_state_change),
                    last_error = COALESCE(?, last_error), missing_count = MIN(missing_count, ?),
                    version = MAX(version, ?) + 1, archived_at = COALESCE(archived_at, ?)
                WHERE site = 'bol' AND external_id = ?
                """,
                (
                    product_id,
                    row[1], row[2], row[3], row[4], row[5], int(bool(row[6])),
                    row[7], row[8], first_seen, last_seen, int(bool(row[11])), row[12],
                    row[13], row[14], row[15], row[16], int(row[17] or 0), int(row[18] or 1),
                    row[19], product_id,
                ),
            )
            conn.execute("DELETE FROM products WHERE site = 'bol' AND external_id = ?", (legacy_id,))

        if "events" in _table_names(conn):
            conn.execute(
                "UPDATE events SET external_id = ? WHERE site = 'bol' AND external_id = ?",
                (product_id, legacy_id),
            )
        if "action_log" in _table_names(conn):
            conn.execute(
                "UPDATE action_log SET external_id = ? WHERE site = 'bol' AND external_id = ?",
                (product_id, legacy_id),
            )


def apply_schema_migrations(conn: sqlite3.Connection, from_version: int) -> None:
    if from_version > LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{from_version} is newer than supported v{LATEST_SCHEMA_VERSION}."
        )
    if from_version < 2:
        _migrate_lifecycle_columns(conn)
    if from_version < 3:
        _migrate_bol_identity(conn)
    conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION}")
