from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from pokemon_parser.config import AppConfig
from pokemon_parser.filters.legacy import load_filters_from_json
from pokemon_parser.storage.backup import backup_sqlite_database, verify_sqlite_database
from pokemon_parser.storage.sqlite import SqliteStorage

logger = logging.getLogger(__name__)

ZERO_ENABLED_FILTERS_WARNING = (
    "No enabled filters loaded. Parser can detect products but will not notify or enqueue workers."
)


@dataclass(frozen=True)
class StartupBootstrapReport:
    db_path: str
    db_exists_before: bool
    db_exists_after: bool
    bak_path: str
    bak_exists: bool
    restored_from_backup: bool
    filters_json_path: str
    filters_json_exists: bool
    scan_settings_path: str
    scan_settings_exists: bool
    scan_settings: dict
    enabled_parser_sites: list[str]
    action_mode: str
    total_filters_before: int
    enabled_filters_before: int
    total_filters_after: int
    enabled_filters_after: int
    imported_filter_count: int
    import_reason: str
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _backup_path_for(db_path: Path) -> Path:
    if db_path.suffix:
        return db_path.with_suffix(".bak")
    return db_path.with_name(f"{db_path.name}.bak")


def _restore_backup_database(*, bak_path: Path, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    verify_sqlite_database(bak_path)
    backup_sqlite_database(bak_path, db_path)


def _filter_counts(storage: SqliteStorage) -> tuple[int, int]:
    total = len(storage.list_filters_all())
    enabled = len(storage.load_filters())
    return total, enabled


def _insert_runtime_log(
    storage: SqliteStorage,
    *,
    level: str,
    message: str,
    details: dict,
) -> None:
    try:
        storage.insert_runtime_log(
            level=level,
            category="startup",
            message=message,
            details=details,
        )
    except Exception:
        logger.exception("startup bootstrap: failed to persist runtime log")


def bootstrap_runtime_storage(
    cfg: AppConfig,
    *,
    sync_filters_json: bool = False,
    log_preflight: bool = True,
) -> tuple[sqlite3.Connection, SqliteStorage, StartupBootstrapReport]:
    db_path = cfg.resolved_db_path()
    bak_path = _backup_path_for(db_path)
    filters_json_path = cfg.filters_json_path
    scan_settings_path = cfg.scan_settings_path()
    db_exists_before = db_path.exists()
    bak_exists = bak_path.exists()
    restored_from_backup = False

    allow_legacy_restore = bool(getattr(cfg, "allow_legacy_backup_restore", False))
    if not db_exists_before and bak_exists and allow_legacy_restore:
        _restore_backup_database(bak_path=bak_path, db_path=db_path)
        restored_from_backup = True
        logger.warning(
            "startup bootstrap: restored missing database from backup db_path=%s bak_path=%s",
            db_path,
            bak_path,
        )

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    storage = SqliteStorage(conn)
    storage.init_schema()

    total_before, enabled_before = _filter_counts(storage)
    imported_filter_count = 0
    import_reason = ""
    filters_json_exists = filters_json_path.exists()

    should_import = False
    if sync_filters_json:
        should_import = filters_json_exists
        import_reason = "sync_filters_json" if should_import else ""
    elif total_before == 0 and filters_json_exists:
        should_import = True
        import_reason = "empty_filter_table"

    if should_import:
        try:
            rules = load_filters_from_json(filters_json_path)
        except Exception:
            if sync_filters_json:
                raise
            logger.exception(
                "startup bootstrap: filters.json auto-import failed path=%s",
                filters_json_path,
            )
            import_reason = "filters_json_import_failed"
        else:
            storage.replace_filters(rules)
            imported_filter_count = len(rules)
            logger.warning(
                "startup bootstrap: imported filters.json reason=%s count=%s path=%s",
                import_reason,
                imported_filter_count,
                filters_json_path,
            )

    total_after, enabled_after = _filter_counts(storage)
    warnings = [ZERO_ENABLED_FILTERS_WARNING] if enabled_after == 0 else []

    report = StartupBootstrapReport(
        db_path=str(db_path),
        db_exists_before=db_exists_before,
        db_exists_after=db_path.exists(),
        bak_path=str(bak_path),
        bak_exists=bak_exists,
        restored_from_backup=restored_from_backup,
        filters_json_path=str(filters_json_path),
        filters_json_exists=filters_json_exists,
        scan_settings_path=str(scan_settings_path),
        scan_settings_exists=scan_settings_path.exists(),
        scan_settings=dict(cfg.scan_settings or {}),
        enabled_parser_sites=list(cfg.enabled_parser_sites()),
        action_mode=cfg.action_mode,
        total_filters_before=total_before,
        enabled_filters_before=enabled_before,
        total_filters_after=total_after,
        enabled_filters_after=enabled_after,
        imported_filter_count=imported_filter_count,
        import_reason=import_reason,
        warnings=warnings,
    )
    details = report.to_dict()

    if log_preflight:
        logger.info(
            "startup preflight: db_path=%s db_exists_before=%s db_exists_after=%s bak_path=%s bak_exists=%s "
            "filters=%s enabled_filters=%s enabled_parser_sites=%s action_mode=%s "
            "filters_json_path=%s filters_json_exists=%s scan_settings_path=%s scan_settings=%s",
            report.db_path,
            report.db_exists_before,
            report.db_exists_after,
            report.bak_path,
            report.bak_exists,
            report.total_filters_after,
            report.enabled_filters_after,
            report.enabled_parser_sites,
            report.action_mode,
            report.filters_json_path,
            report.filters_json_exists,
            report.scan_settings_path,
            json.dumps(report.scan_settings, ensure_ascii=False, sort_keys=True),
        )
        _insert_runtime_log(
            storage,
            level="INFO",
            message="startup preflight completed",
            details=details,
        )

    if restored_from_backup:
        _insert_runtime_log(
            storage,
            level="WARNING",
            message="restored missing database from backup",
            details=details,
        )

    if imported_filter_count:
        _insert_runtime_log(
            storage,
            level="WARNING",
            message=f"imported filters.json count={imported_filter_count} reason={import_reason}",
            details=details,
        )

    if log_preflight:
        for warning in warnings:
            logger.warning("startup bootstrap: %s", warning)
            _insert_runtime_log(
                storage,
                level="WARNING",
                message=warning,
                details=details,
            )

    return conn, storage, report
