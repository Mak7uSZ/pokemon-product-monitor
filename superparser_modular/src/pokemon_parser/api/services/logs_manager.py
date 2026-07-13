from __future__ import annotations

import copy
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any

from pokemon_parser.api.services.shared import storage_context
from pokemon_parser.parsers import SITE_LABELS
from pokemon_parser.utils.logging_setup import debug_log_paths, tail_file

logger = logging.getLogger(__name__)


class LogsManager:
    def __init__(self, *, config_manager):
        self.config_manager = config_manager
        self._summary_cache: dict[str, Any] | None = None
        self._summary_signature: tuple[tuple[str, int, int], ...] | None = None
        self._summary_lock = threading.RLock()
        self._summary_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="logs-summary")
        self._summary_future: Future | None = None

    def _db_signature(self, db_path: Path) -> tuple[tuple[str, int, int], ...]:
        parts: list[tuple[str, int, int]] = []
        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            try:
                stat = path.stat()
            except OSError:
                parts.append((str(path), 0, 0))
            else:
                parts.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(parts)

    @staticmethod
    def _empty_summary(*, warning: str) -> dict[str, Any]:
        return {
            "counts": {"info": 0, "warning": 0, "error": 0},
            "tail": [],
            "cached": False,
            "stale": True,
            "warning": warning,
            "duration_ms": 0,
        }

    def _compute_summary(self) -> tuple[dict[str, Any], float]:
        cfg = self.config_manager.load_app_config()
        started = time.perf_counter()
        with storage_context(cfg) as storage:
            summary = storage.runtime_log_summary()
        return summary, (time.perf_counter() - started) * 1000

    def _refresh_summary_cache(self, signature: tuple[tuple[str, int, int], ...]) -> dict[str, Any]:
        summary, duration_ms = self._compute_summary()
        enriched = {
            **summary,
            "cached": False,
            "stale": False,
            "warning": "",
            "duration_ms": round(duration_ms, 1),
            "generated_at": time.time(),
        }
        with self._summary_lock:
            self._summary_cache = copy.deepcopy(enriched)
            self._summary_signature = signature
            if self._summary_future is not None and self._summary_future.done():
                self._summary_future = None
        logger.info("/api/logs/summary generated in %.1f ms cached=false", duration_ms)
        return enriched

    def _remember_background_result(
        self,
        future: Future,
        signature: tuple[tuple[str, int, int], ...],
    ) -> None:
        try:
            summary = future.result()
        except Exception:
            logger.exception("/api/logs/summary background refresh failed")
            with self._summary_lock:
                if self._summary_future is future:
                    self._summary_future = None
            return

        with self._summary_lock:
            self._summary_cache = copy.deepcopy(summary)
            self._summary_signature = signature
            if self._summary_future is future:
                self._summary_future = None

    def summary(self, *, timeout_seconds: float = 0.8) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        signature = self._db_signature(cfg.resolved_db_path())
        started = time.perf_counter()

        with self._summary_lock:
            if self._summary_cache is not None and self._summary_signature == signature:
                cached = copy.deepcopy(self._summary_cache)
                cached["cached"] = True
                cached["stale"] = False
                cached["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
                logger.info("/api/logs/summary served from cache in %.1f ms", cached["duration_ms"])
                return cached

            future = self._summary_future
            if future is None or future.done():
                future = self._summary_executor.submit(self._refresh_summary_cache, signature)
                future.add_done_callback(lambda done_future: self._remember_background_result(done_future, signature))
                self._summary_future = future
            cached_fallback = copy.deepcopy(self._summary_cache)

        try:
            summary = future.result(timeout=max(0.05, float(timeout_seconds)))
        except TimeoutError:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.warning("/api/logs/summary timed out after %.1f ms; returning fallback", duration_ms)
            if cached_fallback is not None:
                cached_fallback["cached"] = True
                cached_fallback["stale"] = True
                cached_fallback["warning"] = "Log summary is refreshing; showing the last available summary."
                cached_fallback["duration_ms"] = round(duration_ms, 1)
                return cached_fallback
            return self._empty_summary(warning="Log summary is still loading; counts will refresh shortly.")
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception("/api/logs/summary failed after %.1f ms", duration_ms)
            if cached_fallback is not None:
                cached_fallback["cached"] = True
                cached_fallback["stale"] = True
                cached_fallback["warning"] = "Log summary failed to refresh; showing the last available summary."
                cached_fallback["duration_ms"] = round(duration_ms, 1)
                return cached_fallback
            return self._empty_summary(warning="Log summary is temporarily unavailable.")

        summary["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return summary

    def list_logs(
        self,
        *,
        log_type: str | None = None,
        site: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        with storage_context(cfg) as storage:
            items = storage.list_runtime_logs(
                log_type=log_type,
                site=site,
                query=query,
                limit=limit,
            )
        return {
            "items": items,
            "type": (log_type or "").strip().lower() or "all",
            "site": site,
            "query": query or "",
            "sites": [{"site": site_key, "label": label} for site_key, label in SITE_LABELS.items()],
        }

    def debug_files(self, *, lines: int = 200) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        paths = debug_log_paths(cfg.base_dir)
        limit = max(1, min(1000, int(lines)))
        return {
            "lines": limit,
            "files": {
                name: {
                    "path": str(path),
                    "exists": path.exists(),
                    "latest": tail_file(path, lines=limit),
                }
                for name, path in paths.items()
            },
        }
