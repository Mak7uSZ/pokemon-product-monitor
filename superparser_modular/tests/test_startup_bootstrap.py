import json
import logging
import sqlite3
from pathlib import Path

from pokemon_parser.api.services.shared import storage_context
from pokemon_parser.engine.startup import ZERO_ENABLED_FILTERS_WARNING, bootstrap_runtime_storage
from pokemon_parser.filters.models import FilterRule
from pokemon_parser.storage.sqlite import SqliteStorage


class _BootstrapCfg:
    def __init__(self, base_dir: Path, *, allow_legacy_backup_restore: bool = False):
        self.base_dir = base_dir
        self.allow_legacy_backup_restore = allow_legacy_backup_restore
        self.filters_json_path = base_dir / "filters.json"
        self.action_mode = "selenium"
        self.scan_settings = {
            "global": {"max_retries": "3"},
            "sites": {"mediamarkt": {"max_pages": "2"}},
        }

    def resolved_db_path(self) -> Path:
        return self.base_dir / "multi_site_monitor.db"

    def scan_settings_path(self) -> Path:
        return self.base_dir / "scan_settings.json"

    def enabled_parser_sites(self) -> tuple[str, ...]:
        return ("mediamarkt",)


def _enabled_rule(rule_id: int = 1) -> FilterRule:
    return FilterRule(
        id=rule_id,
        name="Pokemon booster",
        sites=("mediamarkt",),
        include_groups=(("pokemon", "booster"),),
        enabled=True,
    )


def _write_filters_json(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "id": 7,
                    "name": "Pokemon booster",
                    "sites": ["mediamarkt"],
                    "keyword_groups": [["pokemon", "booster"]],
                    "enabled": True,
                }
            ]
        ),
        encoding="utf-8",
    )


def test_missing_db_with_backup_restores_db_and_loads_filters(tmp_path):
    bak_path = tmp_path / "multi_site_monitor.bak"
    bak_conn = sqlite3.connect(str(bak_path))
    bak_storage = SqliteStorage(bak_conn)
    bak_storage.init_schema()
    bak_storage.replace_filters([_enabled_rule(11)])
    bak_conn.close()

    cfg = _BootstrapCfg(tmp_path, allow_legacy_backup_restore=True)

    conn, storage, report = bootstrap_runtime_storage(cfg)
    try:
        assert cfg.resolved_db_path().exists()
        assert report.restored_from_backup is True
        assert report.total_filters_after == 1
        assert report.enabled_filters_after == 1
        assert storage.load_filters()[0].id == 11
    finally:
        conn.close()


def test_ui_storage_context_restores_backup_before_creating_empty_db(tmp_path):
    bak_path = tmp_path / "multi_site_monitor.bak"
    bak_conn = sqlite3.connect(str(bak_path))
    bak_storage = SqliteStorage(bak_conn)
    bak_storage.init_schema()
    bak_storage.replace_filters([_enabled_rule(12)])
    bak_conn.close()

    cfg = _BootstrapCfg(tmp_path, allow_legacy_backup_restore=True)

    with storage_context(cfg) as storage:
        filters = storage.load_filters()

    assert cfg.resolved_db_path().exists()
    assert [rule.id for rule in filters] == [12]


def test_empty_db_with_filters_json_auto_imports_without_duplicates(tmp_path):
    cfg = _BootstrapCfg(tmp_path)
    _write_filters_json(cfg.filters_json_path)

    empty_conn = sqlite3.connect(str(cfg.resolved_db_path()))
    SqliteStorage(empty_conn).init_schema()
    empty_conn.close()

    conn, storage, report = bootstrap_runtime_storage(cfg)
    try:
        assert report.import_reason == "empty_filter_table"
        assert report.imported_filter_count == 1
        assert [rule.id for rule in storage.list_filters_all()] == [7]
    finally:
        conn.close()

    second_conn, second_storage, second_report = bootstrap_runtime_storage(cfg)
    try:
        assert second_report.imported_filter_count == 0
        assert [rule.id for rule in second_storage.list_filters_all()] == [7]
    finally:
        second_conn.close()


def test_missing_db_does_not_silently_restore_legacy_backup(tmp_path):
    bak_path = tmp_path / "multi_site_monitor.bak"
    bak_conn = sqlite3.connect(str(bak_path))
    bak_storage = SqliteStorage(bak_conn)
    bak_storage.init_schema()
    bak_storage.replace_filters([_enabled_rule(13)])
    bak_conn.close()

    cfg = _BootstrapCfg(tmp_path)
    conn, storage, report = bootstrap_runtime_storage(cfg)
    try:
        assert report.bak_exists is True
        assert report.restored_from_backup is False
        assert storage.list_filters_all() == []
    finally:
        conn.close()


def test_empty_db_without_filters_json_warns_about_zero_enabled_filters(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    cfg = _BootstrapCfg(tmp_path)

    conn, _storage, report = bootstrap_runtime_storage(cfg)
    try:
        assert report.total_filters_after == 0
        assert report.enabled_filters_after == 0
        assert ZERO_ENABLED_FILTERS_WARNING in report.warnings
        assert ZERO_ENABLED_FILTERS_WARNING in caplog.text
    finally:
        conn.close()
