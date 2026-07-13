from __future__ import annotations

import os
import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4


logger = logging.getLogger(__name__)


def _verify_database(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if row is None or str(row[0]).lower() != "ok":
        raise sqlite3.DatabaseError(f"SQLite integrity_check failed: {row!r}")


def backup_sqlite_connection(
    source: sqlite3.Connection,
    target: Path,
    *,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> Path:
    """Create and verify a consistent online SQLite backup, including WAL state."""
    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    page_size = int(source.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(source.execute("PRAGMA page_count").fetchone()[0])
    source_size_bytes = page_size * page_count
    safe_destination = f"{target.parent.name}/{target.name}"
    logger.info(
        "sqlite backup started source_size_bytes=%s destination=%s",
        source_size_bytes,
        safe_destination,
    )
    if source_size_bytes >= 1024**3:
        logger.warning(
            "large sqlite backup may take several minutes source_size_gib=%.2f destination=%s",
            source_size_bytes / (1024**3),
            safe_destination,
        )
    last_logged_percent = [-5]
    last_logged_at = [0.0]

    def report_progress(status: int, remaining: int, total: int) -> None:
        copied = max(0, total - remaining)
        percent = 100 if total <= 0 else int((copied * 100) / total)
        now = time.monotonic()
        if remaining == 0 or percent >= last_logged_percent[0] + 5 or now - last_logged_at[0] >= 15.0:
            logger.info(
                "sqlite backup progress percent=%s copied_bytes=%s total_bytes=%s remaining_pages=%s",
                percent,
                copied * page_size,
                total * page_size,
                remaining,
            )
            last_logged_percent[0] = percent
            last_logged_at[0] = now
        if progress_callback is not None:
            progress_callback(status, remaining, total)

    try:
        destination = sqlite3.connect(str(temporary))
        try:
            source.backup(destination, pages=2048, progress=report_progress, sleep=0.05)
            logger.info("sqlite backup copy complete destination=%s", safe_destination)
            logger.info("sqlite backup verification started destination=%s", safe_destination)
            _verify_database(destination)
            logger.info("sqlite backup verification complete destination=%s result=ok", safe_destination)
        finally:
            destination.close()
        os.replace(temporary, target)
        logger.info("sqlite backup published destination=%s", safe_destination)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def backup_sqlite_database(source_path: Path, target: Path) -> Path:
    source_path = Path(source_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    source = sqlite3.connect(str(source_path), check_same_thread=False)
    try:
        source.execute("PRAGMA query_only = ON")
        return backup_sqlite_connection(source, target)
    finally:
        source.close()


def verify_sqlite_database(path: Path) -> None:
    conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        _verify_database(conn)
    finally:
        conn.close()
