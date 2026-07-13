from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOG_FILENAMES = {
    "main": "parser_debug.log",
    "errors": "errors.log",
    "selenium_worker": "selenium_worker.log",
    "scan_decisions": "scan_decisions.log",
    "watchlist_tracker": "watchlist_tracker.log",
    "watchlist_decisions": "watchlist_decisions.log",
}

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 10

_HANDLER_MARKER = "_pokemon_parser_debug_handler"
_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|pwd|api[_-]?key|authorization|auth|webhook|"
    r"card[_-]?number|card[_-]?expiry|card[_-]?cvv|cvv)",
    re.IGNORECASE,
)
_BOT_TOKEN_RE = re.compile(r"\bbot\d+:[A-Za-z0-9_-]+\b")
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|pwd|api[_-]?key|authorization|webhook|"
    r"card[_-]?number|card[_-]?expiry|card[_-]?cvv|cvv)=([^&\s,;]+)"
)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class LoggerNameFilter(logging.Filter):
    def __init__(self, prefixes: tuple[str, ...]):
        super().__init__()
        self.prefixes = prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        return any(record.name == prefix or record.name.startswith(f"{prefix}.") for prefix in self.prefixes)


def redact_text(value: str) -> str:
    text = _BOT_TOKEN_RE.sub("bot<redacted>", str(value))
    return _KEY_VALUE_SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                redacted[key] = "<redacted>" if item not in {None, ""} else item
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def resolve_debug_log_dir(base_dir: Path | str | None = None) -> Path:
    root = Path(base_dir).resolve() if base_dir is not None else Path.cwd().resolve()
    return root / "debug_logs"


def debug_log_paths(base_dir: Path | str | None = None) -> dict[str, Path]:
    log_dir = resolve_debug_log_dir(base_dir)
    return {key: log_dir / filename for key, filename in LOG_FILENAMES.items()}


def _debug_file_logging_enabled() -> bool:
    raw = os.environ.get("POKEMON_DEBUG_LOGS", "0").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _level_from_env(default: str | int | None = None) -> int:
    if isinstance(default, int):
        return default
    level_name = str(default or os.environ.get("LOG_LEVEL", "INFO")).upper().strip()
    return int(getattr(logging, level_name, logging.INFO))


def _make_file_handler(path: Path, *, level: int, formatter: logging.Formatter) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=DEFAULT_MAX_BYTES,
        backupCount=DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARKER, True)
    return handler


def _make_console_handler(*, level: int, formatter: logging.Formatter) -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARKER, True)
    return handler


def _remove_marked_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()


def setup_debug_logging(
    base_dir: Path | str | None = None,
    *,
    level: str | int | None = None,
    force: bool = False,
) -> dict[str, Path]:
    """
    Configure process-wide rotating debug logs.

    File logging is disabled by default and can be explicitly enabled with
    POKEMON_DEBUG_LOGS=1. Console logging remains active so CLI/UI starts stay
    readable without creating private diagnostic files.
    """
    log_dir = resolve_debug_log_dir(base_dir)
    paths = debug_log_paths(base_dir)
    effective_level = _level_from_env(level)

    detailed_formatter = RedactingFormatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_formatter = RedactingFormatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root_logger = logging.getLogger()
    managed_loggers = [
        root_logger,
        logging.getLogger("pokemon_parser.scan_decisions"),
        logging.getLogger("pokemon_parser.watchlist_tracker"),
        logging.getLogger("pokemon_parser.watchlist_decisions"),
        logging.getLogger("pokemon_parser.workers"),
        logging.getLogger("pokemon_parser.engine.selenium_dispatcher"),
        logging.getLogger("selenium"),
        logging.getLogger("urllib3"),
    ]

    if force:
        for logger in managed_loggers:
            _remove_marked_handlers(logger)
    elif any(getattr(handler, _HANDLER_MARKER, False) for handler in root_logger.handlers):
        root_logger.setLevel(effective_level)
        return paths

    root_logger.setLevel(effective_level)
    root_logger.addHandler(_make_console_handler(level=effective_level, formatter=console_formatter))

    if _debug_file_logging_enabled():
        log_dir.mkdir(parents=True, exist_ok=True)
        root_logger.addHandler(_make_file_handler(paths["main"], level=effective_level, formatter=detailed_formatter))
        root_logger.addHandler(_make_file_handler(paths["errors"], level=logging.ERROR, formatter=detailed_formatter))

        selenium_handler = _make_file_handler(
            paths["selenium_worker"],
            level=effective_level,
            formatter=detailed_formatter,
        )
        selenium_handler.addFilter(
            LoggerNameFilter(
                (
                    "pokemon_parser.workers",
                    "pokemon_parser.engine.selenium_dispatcher",
                    "selenium",
                    "urllib3",
                )
            )
        )
        root_logger.addHandler(selenium_handler)

        decision_logger = logging.getLogger("pokemon_parser.scan_decisions")
        decision_logger.setLevel(logging.INFO)
        decision_logger.addHandler(
            _make_file_handler(paths["scan_decisions"], level=logging.INFO, formatter=detailed_formatter)
        )
        decision_logger.propagate = True

        watchlist_handler = _make_file_handler(
            paths["watchlist_tracker"],
            level=logging.INFO,
            formatter=detailed_formatter,
        )
        watchlist_handler.addFilter(LoggerNameFilter(("pokemon_parser.watchlist_tracker",)))
        root_logger.addHandler(watchlist_handler)

        watchlist_decision_logger = logging.getLogger("pokemon_parser.watchlist_decisions")
        watchlist_decision_logger.setLevel(logging.INFO)
        watchlist_decision_logger.addHandler(
            _make_file_handler(paths["watchlist_decisions"], level=logging.INFO, formatter=detailed_formatter)
        )
        watchlist_decision_logger.propagate = True

    logging.captureWarnings(True)
    logging.getLogger(__name__).info("debug logging configured log_dir=%s file_logging=%s", log_dir, _debug_file_logging_enabled())
    return paths


def tail_file(path: Path, *, lines: int = 200) -> list[str]:
    max_lines = max(1, min(5000, int(lines)))
    if not path.exists():
        return []

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        file_size = handle.tell()
        block_size = 8192
        buffer = bytearray()
        blocks = 0

        while file_size > 0 and buffer.count(b"\n") <= max_lines:
            blocks += 1
            read_size = min(block_size, file_size)
            file_size -= read_size
            handle.seek(file_size)
            buffer[:0] = handle.read(read_size)
            if blocks > 256:
                break

    text = buffer.decode("utf-8", errors="replace")
    return text.splitlines()[-max_lines:]
