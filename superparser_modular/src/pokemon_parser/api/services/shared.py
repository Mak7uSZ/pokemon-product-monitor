from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from pokemon_parser.config import AppConfig
from pokemon_parser.engine.startup import bootstrap_runtime_storage
from pokemon_parser.storage.sqlite import SqliteStorage


@dataclass(frozen=True)
class AppPaths:
    repo_root: Path
    app_root: Path
    frontend_root: Path
    frontend_dist: Path


def _discover_repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current.parent, *current.parents):
        if (candidate / "superparser_modular").exists() and (candidate / "frontend").exists():
            return candidate
    return current.parents[5]


@lru_cache(maxsize=1)
def get_paths() -> AppPaths:
    repo_root = _discover_repo_root(Path(__file__))
    app_root = repo_root / "superparser_modular"
    frontend_root = repo_root / "frontend"
    return AppPaths(
        repo_root=repo_root,
        app_root=app_root,
        frontend_root=frontend_root,
        frontend_dist=frontend_root / "dist",
    )


@contextmanager
def storage_context(cfg: AppConfig) -> Iterator[SqliteStorage]:
    conn, storage, _report = bootstrap_runtime_storage(cfg, log_preflight=False)
    try:
        yield storage
    finally:
        conn.close()


@lru_cache(maxsize=1)
def get_config_manager():
    from pokemon_parser.api.services.config_manager import ConfigManager

    return ConfigManager(get_paths().app_root)


@lru_cache(maxsize=1)
def get_runtime_manager():
    from pokemon_parser.api.services.runtime_manager import RuntimeManager

    return RuntimeManager(paths=get_paths(), config_manager=get_config_manager())


@lru_cache(maxsize=1)
def get_filters_manager():
    from pokemon_parser.api.services.filters_manager import FiltersManager

    return FiltersManager(config_manager=get_config_manager())


@lru_cache(maxsize=1)
def get_logs_manager():
    from pokemon_parser.api.services.logs_manager import LogsManager

    return LogsManager(config_manager=get_config_manager())


@lru_cache(maxsize=1)
def get_parser_settings_manager():
    from pokemon_parser.api.services.parser_settings_manager import ParserSettingsManager

    return ParserSettingsManager(
        config_manager=get_config_manager(),
        runtime_manager=get_runtime_manager(),
    )


@lru_cache(maxsize=1)
def get_watchlist_manager():
    from pokemon_parser.api.services.watchlist_manager import WatchlistManager

    return WatchlistManager(
        config_manager=get_config_manager(),
        runtime_manager=get_runtime_manager(),
    )


@lru_cache(maxsize=1)
def get_settings_backup_manager():
    from pokemon_parser.api.services.settings_backup_manager import SettingsBackupManager

    return SettingsBackupManager(
        config_manager=get_config_manager(),
        paths=get_paths(),
    )
