import sqlite3
from pathlib import Path

from pokemon_parser.models import ParsedItem
from pokemon_parser.storage.backup import backup_sqlite_database
from pokemon_parser.storage.migrations import LATEST_SCHEMA_VERSION
from pokemon_parser.storage.sqlite import SqliteStorage


def test_upsert_new_item_event():
    conn = sqlite3.connect(":memory:")
    storage = SqliteStorage(conn)
    storage.init_schema()

    events = storage.upsert_items([
        ParsedItem(
            site="pocketgames",
            external_id="a",
            title="Pokemon Booster",
            title_norm="pokemon booster",
            url="https://pocketgames.nl/products/a",
            price_value=10.0,
            availability_text="available",
            is_available=True,
            seller="pocketgames",
        )
    ])
    assert any(event.event_type == "new_item" for event in events)


def test_upsert_price_changed():
    conn = sqlite3.connect(":memory:")
    storage = SqliteStorage(conn)
    storage.init_schema()

    storage.upsert_items([
        ParsedItem(
            site="bol",
            external_id="x",
            title="T",
            title_norm="t",
            url="u",
            price_value=10.0,
            availability_text=None,
            is_available=False,
            seller="bol",
        )
    ])

    events = storage.upsert_items([
        ParsedItem(
            site="bol",
            external_id="x",
            title="T",
            title_norm="t",
            url="u",
            price_value=12.0,
            availability_text=None,
            is_available=False,
            seller="bol",
        )
    ])
    assert any(event.event_type == "price_changed" for event in events)


def test_runtime_log_summary_counts_and_tail():
    conn = sqlite3.connect(":memory:")
    storage = SqliteStorage(conn)
    storage.init_schema()

    storage.insert_runtime_log(level="INFO", category="runtime", message="started")
    storage.insert_runtime_log(level="WARNING", category="runtime", message="recoverable")
    storage.insert_runtime_log(level="ERROR", category="error", message="failed")

    summary = storage.runtime_log_summary(tail_limit=2)

    assert summary["counts"] == {"info": 1, "warning": 1, "error": 1}
    assert [item["message"] for item in summary["tail"]] == ["recoverable", "failed"]


def test_runtime_log_indexes_exist():
    conn = sqlite3.connect(":memory:")
    storage = SqliteStorage(conn)
    storage.init_schema()

    index_rows = conn.execute("PRAGMA index_list(runtime_logs)").fetchall()
    index_names = {row[1] for row in index_rows}

    assert "idx_runtime_logs_level" in index_names
    assert "idx_runtime_logs_category_id" in index_names
    assert "idx_runtime_logs_site_id" in index_names


def test_online_backup_includes_committed_wal_rows(tmp_path: Path):
    source_path = tmp_path / "source.db"
    backup_path = tmp_path / "backup.db"
    conn = sqlite3.connect(str(source_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE values_table (value TEXT)")
    conn.execute("INSERT INTO values_table VALUES ('committed-in-wal')")
    conn.commit()

    backup_sqlite_database(source_path, backup_path)

    with sqlite3.connect(str(backup_path)) as backup:
        assert backup.execute("SELECT value FROM values_table").fetchone() == ("committed-in-wal",)
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()


def test_legacy_bol_identity_migration_is_backed_up_and_rewrites_references(tmp_path: Path):
    database_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(database_path))
    conn.executescript(
        """
        CREATE TABLE products (
            site TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT, title_norm TEXT,
            url TEXT, price_value REAL, availability_text TEXT, is_available INTEGER,
            seller TEXT, extra_json TEXT, first_seen TEXT, last_seen TEXT, active INTEGER,
            PRIMARY KEY (site, external_id)
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, site TEXT, external_id TEXT, event_type TEXT,
            old_value TEXT, new_value TEXT, matched_filter_ids_json TEXT, created_at TEXT
        );
        CREATE TABLE action_log (
            id INTEGER PRIMARY KEY, site TEXT, external_id TEXT, action_type TEXT,
            action_case TEXT, status TEXT, details TEXT, created_at TEXT
        );
        INSERT INTO products VALUES (
            'bol', 'pokemon-set_9300000123456789', 'Pokemon Set', 'pokemon set',
            'https://www.bol.com/nl/nl/p/pokemon-set/9300000123456789/', 12.5,
            'available', 1, 'bol', '{"product_id":"9300000123456789"}',
            '2025-01-01T00:00:00+00:00', '2025-01-02T00:00:00+00:00', 1
        );
        INSERT INTO events VALUES (1, 'bol', 'pokemon-set_9300000123456789', 'new_item', NULL, NULL, '[]', '2025-01-01');
        INSERT INTO action_log VALUES (1, 'bol', 'pokemon-set_9300000123456789', 'notify', NULL, 'success', NULL, '2025-01-01');
        PRAGMA user_version = 1;
        """
    )
    conn.commit()

    storage = SqliteStorage(conn)
    storage.init_schema()

    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    assert conn.execute("SELECT external_id, canonical_id FROM products WHERE site = 'bol'").fetchone() == (
        "9300000123456789",
        "9300000123456789",
    )
    assert conn.execute("SELECT external_id FROM events").fetchone() == ("9300000123456789",)
    assert conn.execute("SELECT external_id FROM action_log").fetchone() == ("9300000123456789",)
    backup_path = Path(storage.last_migration_backup_path or "")
    assert backup_path.is_file()
    with sqlite3.connect(str(backup_path)) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()


def test_product_lifecycle_survives_restart_and_failure(tmp_path: Path):
    database_path = tmp_path / "lifecycle.db"
    item = ParsedItem(
        site="bol",
        external_id="9300000123456789",
        title="Pokemon Persisted",
        title_norm="pokemon persisted",
        url="https://www.bol.com/nl/nl/p/pokemon-persisted/9300000123456789/",
        price_value=20.0,
        availability_text="available",
        is_available=True,
        seller="bol",
        extra={"product_id": "9300000123456789"},
    )
    first_conn = sqlite3.connect(str(database_path))
    first_storage = SqliteStorage(first_conn)
    first_storage.init_schema()
    assert [event.event_type for event in first_storage.upsert_items([item], reconcile_missing=False)] == ["new_item"]
    assert first_storage.upsert_items([item], reconcile_missing=False) == []
    first_conn.close()

    second_conn = sqlite3.connect(str(database_path))
    try:
        second_storage = SqliteStorage(second_conn)
        second_storage.init_schema()
        before = second_storage.list_products(site="bol")[0]
        assert before["title"] == "Pokemon Persisted"
        assert before["lifecycle_state"] == "active"
        assert before["last_successfully_parsed"]

        assert second_storage.record_product_check_failure("bol", item.external_id, "temporary browser failure")
        failed = second_storage.list_products(site="bol")[0]
        assert failed["title"] == before["title"]
        assert failed["url"] == before["url"]
        assert failed["last_successfully_parsed"] == before["last_successfully_parsed"]
        assert failed["last_error"] == "temporary browser failure"

        assert second_storage.archive_product("bol", item.external_id)
        assert second_storage.list_products(site="bol") == []
        archived = second_storage.list_products(site="bol", include_archived=True)[0]
        assert archived["lifecycle_state"] == "archived"
        assert archived["archived_at"]
    finally:
        second_conn.close()


def test_watchlist_state_returns_consistent_items_summary_and_revision():
    from pokemon_parser.models import WatchlistProduct

    conn = sqlite3.connect(":memory:")
    storage = SqliteStorage(conn)
    storage.init_schema()
    storage.upsert_watchlist_entry(
        WatchlistProduct(
            site="bol",
            product_key="9300000123456789",
            title="Pokemon Watchlist",
            url="https://www.bol.com/nl/nl/p/pokemon-watchlist/9300000123456789/",
            current_inventory_status="in_stock",
            status_confidence_score=1.0,
        )
    )

    state = storage.watchlist_state()

    assert len(state["items"]) == 1
    assert state["summary"]["total"] == 1
    assert state["summary"]["available"] == 1
    assert state["revision"].endswith(":1")
    conn.close()
