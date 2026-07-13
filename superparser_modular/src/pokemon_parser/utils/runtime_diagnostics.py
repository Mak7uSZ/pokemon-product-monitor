from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from pokemon_parser.utils.logging_setup import redact_secrets

logger = logging.getLogger(__name__)


def _path_candidates(names: tuple[str, ...]) -> dict[str, str]:
    return {name: (shutil.which(name) or "") for name in names}


def log_startup_diagnostics(
    *,
    cfg: Any,
    project_root: Path | str | None = None,
    startup_report: Any | None = None,
) -> None:
    report = startup_report.to_dict() if hasattr(startup_report, "to_dict") else {}
    enabled_sites = cfg.enabled_parser_sites() if callable(getattr(cfg, "enabled_parser_sites", None)) else ()
    diagnostics = {
        "python_version": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "os_name": os.name,
        "cwd": str(Path.cwd()),
        "project_root": str(project_root or getattr(cfg, "base_dir", "")),
        "pythonpath": os.environ.get("PYTHONPATH", ""),
        "path_chrome_locations": _path_candidates(
            ("chrome.exe", "chrome", "google-chrome", "chromium", "chromedriver.exe", "chromedriver")
        ),
        "env_path": str(getattr(cfg, "env_path", "")),
        "env_exists": bool(getattr(cfg, "env_path", Path()).exists()),
        "db_path": report.get("db_path") or str(cfg.resolved_db_path()),
        "filter_counts": {
            "total_before": report.get("total_filters_before"),
            "enabled_before": report.get("enabled_filters_before"),
            "total_after": report.get("total_filters_after"),
            "enabled_after": report.get("enabled_filters_after"),
        },
        "scan_settings_path": report.get("scan_settings_path") or str(cfg.scan_settings_path()),
        "scan_settings_exists": report.get("scan_settings_exists"),
        "scan_settings": report.get("scan_settings", getattr(cfg, "scan_settings", {})),
        "enabled_parsers": list(enabled_sites),
        "action_mode": getattr(cfg, "action_mode", ""),
        "notifications": {
            "global_enabled": bool(getattr(cfg, "enable_notifications", False)),
            "telegram_enabled": bool(getattr(cfg, "telegram_bot_token", "") and getattr(cfg, "telegram_chat_id", "")),
            "discord_enabled": False,
            "heartbeat_alerts": bool(getattr(cfg, "enable_heartbeat_alerts", False)),
            "success_alerts": bool(getattr(cfg, "enable_success_alerts", False)),
            "error_alerts": bool(getattr(cfg, "enable_error_alerts", False)),
        },
    }
    logger.info(
        "startup diagnostics: %s",
        json.dumps(redact_secrets(diagnostics), ensure_ascii=False, sort_keys=True),
    )
