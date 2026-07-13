from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pokemon_parser.utils.logging_setup import redact_secrets, resolve_debug_log_dir


LIKELY_CHROME_FAILURE_CAUSES = (
    "ChromeDriver mismatch with installed Chrome",
    "Chrome profile lock or stale SingletonLock",
    "Wrong --user-data-dir or --profile-directory",
    "Chrome already running with the same profile",
    "Bad Chrome option or unsupported argument",
    "Filesystem permissions issue on the Chrome profile or debug log directory",
)


@dataclass(frozen=True)
class SeleniumDiagnostics:
    chrome_binary_path: str
    chrome_version: str
    chromedriver_path: str
    chromedriver_version: str
    chrome_options: list[str]
    chrome_experimental_options: dict[str, Any]
    user_data_dir: str
    profile_dir: str
    profile_path: str
    profile_path_exists: bool
    singleton_lock_exists: bool
    chrome_process_count: int
    chromedriver_process_count: int
    headless: bool
    remote_debugging_port: str
    chromedriver_verbose_log: str
    platform: str = field(default_factory=platform.platform)
    python: str = field(default_factory=lambda: sys.version.replace("\n", " "))

    def to_dict(self) -> dict[str, Any]:
        return redact_secrets(
            {
                "chrome_binary_path": self.chrome_binary_path,
                "chrome_version": self.chrome_version,
                "chromedriver_path": self.chromedriver_path,
                "chromedriver_version": self.chromedriver_version,
                "chrome_options": list(self.chrome_options),
                "chrome_experimental_options": dict(self.chrome_experimental_options),
                "user_data_dir": self.user_data_dir,
                "profile_dir": self.profile_dir,
                "profile_path": self.profile_path,
                "profile_path_exists": self.profile_path_exists,
                "singleton_lock_exists": self.singleton_lock_exists,
                "chrome_process_count": self.chrome_process_count,
                "chromedriver_process_count": self.chromedriver_process_count,
                "headless": self.headless,
                "remote_debugging_port": self.remote_debugging_port,
                "chromedriver_verbose_log": self.chromedriver_verbose_log,
                "platform": self.platform,
                "python": self.python,
            }
        )


def timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def safe_filename_part(value: str, *, fallback: str = "unknown") -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or ""))
    text = "_".join(part for part in text.split("_") if part)
    return text[:80] or fallback


def run_version_command(path: str | None) -> str:
    if not path:
        return ""
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (completed.stdout or completed.stderr or "").strip()
    except Exception as exc:
        return f"version_probe_failed: {type(exc).__name__}: {exc}"


def resolve_chrome_binary(configured: str | None) -> str:
    candidates = [
        configured or "",
        shutil.which("chrome.exe") or "",
        shutil.which("chrome") or "",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    return configured or ""


def count_processes(process_name: str) -> int:
    lowered = process_name.lower()
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
            return sum(1 for line in lines if lowered in line.lower())

        completed = subprocess.run(
            ["pgrep", "-f", process_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return len([line for line in (completed.stdout or "").splitlines() if line.strip()])
    except Exception:
        return -1


def _profile_path(user_data_dir: str, profile_dir: str) -> Path | None:
    if not user_data_dir:
        return None
    root = Path(user_data_dir)
    return root / profile_dir if profile_dir else root


def _remote_debugging_port(arguments: list[str]) -> str:
    for argument in arguments:
        if argument.startswith("--remote-debugging-port="):
            return argument.split("=", 1)[1]
    return ""


def collect_selenium_diagnostics(
    cfg: Any,
    *,
    options: Any,
    chromedriver_log_path: Path,
    base_dir: Path | str | None = None,
) -> SeleniumDiagnostics:
    arguments = list(getattr(options, "arguments", []) or [])
    experimental_options = dict(getattr(options, "experimental_options", {}) or {})
    chrome_binary = resolve_chrome_binary(getattr(options, "binary_location", "") or getattr(cfg, "chrome_binary", ""))
    chromedriver_path = shutil.which("chromedriver") or shutil.which("chromedriver.exe") or ""
    user_data_dir = str(getattr(cfg, "chrome_user_data_dir", "") or "")
    profile_dir = str(getattr(cfg, "chrome_profile_dir", "") or "")
    profile_path = _profile_path(user_data_dir, profile_dir)
    singleton_lock = (Path(user_data_dir) / "SingletonLock") if user_data_dir else None

    return SeleniumDiagnostics(
        chrome_binary_path=chrome_binary,
        chrome_version=run_version_command(chrome_binary),
        chromedriver_path=chromedriver_path or "selenium_manager",
        chromedriver_version=run_version_command(chromedriver_path) if chromedriver_path else "selenium_manager",
        chrome_options=arguments,
        chrome_experimental_options=experimental_options,
        user_data_dir=user_data_dir,
        profile_dir=profile_dir,
        profile_path=str(profile_path or ""),
        profile_path_exists=bool(profile_path and profile_path.exists()),
        singleton_lock_exists=bool(singleton_lock and singleton_lock.exists()),
        chrome_process_count=count_processes("chrome.exe" if os.name == "nt" else "chrome"),
        chromedriver_process_count=count_processes("chromedriver.exe" if os.name == "nt" else "chromedriver"),
        headless=any(argument == "--headless" or argument.startswith("--headless=") for argument in arguments),
        remote_debugging_port=_remote_debugging_port(arguments),
        chromedriver_verbose_log=str(chromedriver_log_path),
    )


def write_selenium_startup_failure_snapshot(
    *,
    cfg: Any,
    diagnostics: SeleniumDiagnostics,
    exc: BaseException,
    base_dir: Path | str | None = None,
) -> Path:
    log_dir = resolve_debug_log_dir(base_dir or getattr(cfg, "base_dir", None))
    log_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = log_dir / f"selenium_startup_failure_{timestamp_for_filename()}.txt"
    payload = {
        "failure": f"{type(exc).__name__}: {exc}",
        "diagnostics": diagnostics.to_dict(),
        "likely_causes": list(LIKELY_CHROME_FAILURE_CAUSES),
        "traceback": traceback.format_exc(),
    }
    snapshot_path.write_text(
        json.dumps(redact_secrets(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snapshot_path
