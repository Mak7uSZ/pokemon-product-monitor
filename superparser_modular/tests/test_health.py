import sqlite3

from pokemon_parser.api.routes.health import live, readiness_snapshot
from pokemon_parser.storage.sqlite import SqliteStorage


def test_liveness_has_no_external_dependencies():
    assert live() == {"ok": True, "status": "alive"}


def test_readiness_requires_current_database_and_frontend(tmp_path):
    database = tmp_path / "app.db"
    frontend_index = tmp_path / "dist" / "index.html"
    frontend_index.parent.mkdir()
    frontend_index.write_text("dashboard", encoding="utf-8")
    conn = sqlite3.connect(str(database))
    SqliteStorage(conn).init_schema()
    conn.close()

    ready, payload = readiness_snapshot(database_path=database, frontend_index=frontend_index)

    assert ready is True
    assert payload["checks"]["database"]["ok"] is True
    assert payload["checks"]["frontend"]["ok"] is True


def test_readiness_fails_without_mutating_missing_database(tmp_path):
    database = tmp_path / "missing.db"
    ready, payload = readiness_snapshot(
        database_path=database,
        frontend_index=tmp_path / "missing-index.html",
    )

    assert ready is False
    assert payload["status"] == "not_ready"
    assert database.exists() is False
