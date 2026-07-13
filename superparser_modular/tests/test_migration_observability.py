import json
import logging
import sqlite3
import time

import pytest

import pokemon_parser.storage.migrations as migrations
from pokemon_parser.storage.backup import backup_sqlite_connection
from pokemon_parser.storage.migrations import MIGRATION_LOCK_STALE_SECONDS, migration_process_lock
from pokemon_parser.storage.sqlite import SqliteStorage


def test_sqlite_backup_reports_copy_and_verification_progress(tmp_path, caplog):
    source = sqlite3.connect(str(tmp_path / "source.db"))
    try:
        source.execute("CREATE TABLE sample (value TEXT)")
        source.executemany("INSERT INTO sample(value) VALUES (?)", [(f"row-{index}",) for index in range(100)])
        source.commit()
        progress = []
        caplog.set_level(logging.INFO)

        target = backup_sqlite_connection(
            source,
            tmp_path / "backups" / "verified.db",
            progress_callback=lambda status, remaining, total: progress.append((status, remaining, total)),
        )

        assert target.is_file()
        assert progress
        assert progress[-1][1] == 0
        assert "sqlite backup started" in caplog.text
        assert "sqlite backup progress" in caplog.text
        assert "sqlite backup verification started" in caplog.text
        assert "sqlite backup verification complete" in caplog.text
        assert "sqlite backup published" in caplog.text
        assert str(tmp_path) not in caplog.text
    finally:
        source.close()


def test_migration_process_lock_rejects_another_active_process(tmp_path):
    database_path = tmp_path / "app.db"
    first = sqlite3.connect(str(database_path))
    second = sqlite3.connect(str(database_path))
    try:
        with migration_process_lock(first):
            with pytest.raises(RuntimeError, match="Another startup/migration process is active"):
                with migration_process_lock(second):
                    pass
    finally:
        first.close()
        second.close()


def test_recent_dead_process_lock_is_not_removed_automatically(tmp_path, monkeypatch):
    database_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(database_path))
    lock_path = database_path.with_name(f"{database_path.name}.startup-migration.lock")
    lock_path.write_text(
        json.dumps({"pid": 987654, "created_epoch": time.time(), "database_name": database_path.name}),
        encoding="utf-8",
    )
    monkeypatch.setattr(migrations, "_pid_is_running", lambda pid: False)
    try:
        with pytest.raises(RuntimeError, match="recent startup/migration lock"):
            with migration_process_lock(conn):
                pass
        assert lock_path.is_file()
    finally:
        conn.close()


def test_stale_dead_lock_allows_only_exact_abandoned_temp_cleanup(tmp_path, monkeypatch):
    database_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(database_path))
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    abandoned = backup_dir / (
        ".app-pre-migration-v0-to-v3-20260713T120000Z.db."
        "0123456789abcdef0123456789abcdef.tmp"
    )
    abandoned_journal = abandoned.with_name(f"{abandoned.name}-journal")
    unrelated = backup_dir / ".unrelated.db.0123456789abcdef0123456789abcdef.tmp"
    for path in (abandoned, abandoned_journal, unrelated):
        path.write_text("temporary", encoding="utf-8")

    lock_path = database_path.with_name(f"{database_path.name}.startup-migration.lock")
    lock_path.write_text(
        json.dumps(
            {
                "pid": 987654,
                "created_epoch": time.time() - MIGRATION_LOCK_STALE_SECONDS - 1,
                "database_name": database_path.name,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(migrations, "_pid_is_running", lambda pid: False)
    try:
        with migration_process_lock(conn) as acquired:
            assert acquired.recovered_stale_lock is True
            assert acquired.previous_owner_pid == 987654
            assert acquired.path == lock_path
            assert lock_path.is_file()
            assert not abandoned.exists()
            assert not abandoned_journal.exists()
            assert unrelated.is_file()
        assert not lock_path.exists()
    finally:
        conn.close()


def test_schema_initialization_logs_backup_verification_and_migration(tmp_path, caplog):
    database_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(database_path))
    try:
        storage = SqliteStorage(conn)
        storage.init_schema()
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        caplog.set_level(logging.INFO)
        caplog.clear()

        storage.init_schema()

        assert storage.last_migration_backup_path is not None
        assert "sqlite schema initialization started" in caplog.text
        assert "pre-migration backup verified" in caplog.text
        assert "sqlite schema migration started" in caplog.text
        assert "sqlite schema migration complete" in caplog.text
        assert "sqlite schema initialization complete" in caplog.text
        assert not database_path.with_name(f"{database_path.name}.startup-migration.lock").exists()
    finally:
        conn.close()


def test_in_memory_database_does_not_create_process_lock(tmp_path):
    conn = sqlite3.connect(":memory:")
    try:
        with migration_process_lock(conn) as acquired:
            assert acquired.path is None
        assert list(tmp_path.glob("*.startup-migration.lock")) == []
    finally:
        conn.close()
