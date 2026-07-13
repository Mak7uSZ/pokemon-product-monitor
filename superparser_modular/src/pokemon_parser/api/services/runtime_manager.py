from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pokemon_parser.engine.antiban_policies import build_antiban
from pokemon_parser.engine.heartbeat import heartbeat_loop
from pokemon_parser.engine.pipeline import Pipeline
from pokemon_parser.engine.runtime_state import RuntimeStateStore, WatchlistRuntimeState
from pokemon_parser.engine.selenium_dispatcher import SeleniumDispatcher
from pokemon_parser.engine.startup import StartupBootstrapReport, bootstrap_runtime_storage
from pokemon_parser.notifications.telegram import TelegramNotifier
from pokemon_parser.parsers import SITE_LABELS
from pokemon_parser.storage.backup import backup_sqlite_database
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.utils.runtime_diagnostics import log_startup_diagnostics
from pokemon_parser.utils.time import utc_now_iso
from pokemon_parser.workers.selenium_worker import SeleniumWorker

logger = logging.getLogger(__name__)


@dataclass
class RuntimeExecutionContext:
    cfg: Any
    conn: sqlite3.Connection
    storage: SqliteStorage
    antiban: Any
    runtime_state: RuntimeStateStore
    selenium_queue: queue.Queue
    selenium_dispatcher: SeleniumDispatcher
    selenium_worker: SeleniumWorker | None
    pipeline: Pipeline
    watchlist_state: WatchlistRuntimeState
    startup_report: StartupBootstrapReport


class RuntimeManager:
    def __init__(self, *, paths, config_manager):
        self.paths = paths
        self.config_manager = config_manager

        self._lock = threading.RLock()
        self._current_future: concurrent.futures.Future | None = None
        self._current_kind: str | None = None
        self._current_context: RuntimeExecutionContext | None = None
        self._last_started_at: str | None = None
        self._last_finished_at: str | None = None
        self._last_exit_code: int | None = None
        self._last_error: str = ""
        self._last_runtime_snapshot: dict[str, Any] | None = None

        self._timer_last_run_at: str | None = None
        self._timer_next_run_epoch: float | None = None
        self._timer_last_error: str = ""
        self._close_requested = False
        self._process_exit_scheduled = False

        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._loop_thread = threading.Thread(target=self._run_event_loop, name="runtime-manager-loop", daemon=True)
        self._loop_thread.start()
        self._loop_ready.wait(timeout=5)

        self._timer_stop = threading.Event()
        self._timer_thread = threading.Thread(target=self._timer_loop, name="runtime-manager-timer", daemon=True)
        self._timer_thread.start()

    def _run_event_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    @staticmethod
    def _iso_from_epoch(value: float | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _uptime_seconds(started_at: str | None) -> int:
        if not started_at:
            return 0
        try:
            normalized = started_at[:-1] + "+00:00" if started_at.endswith("Z") else started_at
            started = datetime.fromisoformat(normalized)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
        except Exception:
            return 0

    def _is_running_locked(self) -> bool:
        return self._current_future is not None and not self._current_future.done()

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running_locked()

    def _schedule_runtime(self, *, kind: str, trigger: str) -> bool:
        with self._lock:
            if self._close_requested:
                logger.info("[runtime-manager] runtime start ignored because shutdown was requested")
                return False
            if self._is_running_locked():
                logger.info("runtime_start_ignored_already_running")
                return False
            self._last_started_at = utc_now_iso()
            self._last_error = ""
            self._current_kind = kind
            future = asyncio.run_coroutine_threadsafe(self._runtime_main(kind=kind, trigger=trigger), self._loop)
            future.add_done_callback(self._handle_runtime_done)
            self._current_future = future
            return True

    def start(self) -> dict[str, Any]:
        scheduled = self._schedule_runtime(kind="continuous", trigger="manual")
        status = self.status()
        if not scheduled and status.get("running"):
            status["message"] = "Runtime already running."
        return status

    def run_once_now(self, *, trigger: str = "manual") -> dict[str, Any]:
        scheduled = self._schedule_runtime(kind="once", trigger=trigger)
        status = self.status()
        if not scheduled and status.get("running"):
            status["message"] = "Runtime already running."
        return status

    def stop(self) -> dict[str, Any]:
        with self._lock:
            future = self._current_future
            context = self._current_context
        if future is None:
            return self.status()
        future.cancel()
        if context is not None:
            context.selenium_dispatcher.stop(timeout=5)
        try:
            future.result(timeout=10)
        except Exception:
            pass
        if context is not None:
            context.selenium_dispatcher.stop(timeout=2)
        return self.status()

    def restart(self) -> dict[str, Any]:
        stopped_status = self.stop()
        if stopped_status.get("running"):
            stopped_status["message"] = "Runtime is still stopping; restart was not started yet."
            return stopped_status
        self.start()
        return self.status()

    def restart_if_running(self, *, reason: str) -> bool:
        logger.info("[runtime-manager] restart_if_running reason=%s", reason)
        if not self.is_running():
            return False
        self.restart()
        return True

    def _register_context(self, context: RuntimeExecutionContext) -> None:
        with self._lock:
            self._current_context = context

    @staticmethod
    def _default_endpoint_statuses() -> dict[str, dict[str, Any]]:
        return {
            "mediamarkt": {
                "graphql_circuit_open": False,
                "graphql_backoff_until": None,
                "discovery_routing_mode": "normal",
                "graphql_endpoint_status": "active",
                "graphql_consecutive_quota_denies": 0,
            }
        }

    @staticmethod
    def _apply_endpoint_statuses(overview: dict[str, Any], endpoint_statuses: dict[str, dict[str, Any]]) -> None:
        overview["endpoint_statuses"] = deepcopy(endpoint_statuses)
        site_states = overview.get("site_states")
        if not isinstance(site_states, dict):
            return
        for site, endpoint_status in endpoint_statuses.items():
            if site in site_states and isinstance(endpoint_status, dict):
                site_states[site].update(deepcopy(endpoint_status))

    def _record_runtime_snapshot(self, context: RuntimeExecutionContext) -> None:
        selenium_snapshot = self._build_selenium_snapshot(context.selenium_dispatcher, context.cfg)
        overview = context.runtime_state.snapshot_overview(
            queue_size=context.selenium_dispatcher.counts().get("total", 0),
            selenium=selenium_snapshot,
        )
        self._apply_endpoint_statuses(overview, context.pipeline.endpoint_status_snapshot())
        overview["watchlist"] = context.watchlist_state.snapshot(storage=context.storage)
        overview["watchlist_lifecycle"] = self._build_watchlist_lifecycle_snapshot(overview["watchlist"])
        overview["startup_preflight"] = context.startup_report.to_dict()
        overview["warnings"] = list(context.startup_report.warnings)
        if selenium_snapshot.get("last_error"):
            overview["warnings"].append(f"Selenium worker error: {selenium_snapshot.get('last_error', '')}")
        with self._lock:
            self._last_runtime_snapshot = overview

    def current_selenium_dispatcher(self) -> SeleniumDispatcher | None:
        with self._lock:
            if not self._is_running_locked() or self._current_context is None:
                return None
            return self._current_context.selenium_dispatcher

    @staticmethod
    def _select_watchlist_warm_tab_items(storage: SqliteStorage, cfg: Any) -> list[dict[str, Any]]:
        if not bool(getattr(cfg, "watchlist_warm_tabs_enabled", False)):
            return []
        max_tabs = max(1, int(getattr(cfg, "watchlist_warm_tabs_max", 6) or 6))
        try:
            items = storage.list_watchlist(enabled=True, limit=2000)
        except Exception:
            logger.exception("[runtime-manager] failed to load watchlist items for warm tabs")
            return []

        selected: list[dict[str, Any]] = []
        for item in items:
            site = str(item.get("site") or "").strip().lower()
            if site == "mediamarkt" and not bool(getattr(cfg, "mediamarkt_warm_tabs_enabled", True)):
                continue
            high_priority = site == "mediamarkt" or bool(item.get("pinned")) or bool(item.get("matched_filter_ids"))
            if not high_priority or not item.get("url"):
                continue
            selected.append(item)
            if len(selected) >= max_tabs:
                break
        return selected

    def _handle_runtime_done(self, future: concurrent.futures.Future) -> None:
        exit_code = 0
        last_error = ""
        try:
            future.result()
        except asyncio.CancelledError:
            exit_code = 0
        except concurrent.futures.CancelledError:
            exit_code = 0
        except Exception as exc:
            exit_code = 1
            last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[runtime-manager] runtime task failed")

        with self._lock:
            if self._current_future is future:
                self._current_future = None
                self._current_kind = None
                self._current_context = None
            self._last_exit_code = exit_code
            self._last_finished_at = utc_now_iso()
            if last_error:
                self._last_error = last_error

    @staticmethod
    def _selenium_config_snapshot(cfg: Any) -> dict[str, Any]:
        if hasattr(cfg, "selenium_runtime_config"):
            try:
                return dict(cfg.selenium_runtime_config())
            except Exception:
                pass
        return {
            "action_mode": getattr(cfg, "action_mode", ""),
            "legacy_prewarm": bool(getattr(cfg, "selenium_prewarm", False)),
            "prewarm_enabled": bool(
                getattr(cfg, "selenium_prewarm_enabled", getattr(cfg, "selenium_prewarm", False))
            ),
            "prewarm_on_runtime_start": bool(getattr(cfg, "selenium_prewarm_on_runtime_start", True)),
            "keep_browser_alive": bool(getattr(cfg, "selenium_keep_browser_alive", True)),
            "warm_tabs_enabled": bool(getattr(cfg, "watchlist_warm_tabs_enabled", False)),
            "warm_tabs_max": int(getattr(cfg, "watchlist_warm_tabs_max", 0) or 0),
            "challenge_cooldown_base_seconds": float(getattr(cfg, "challenge_cooldown_base_seconds", 30.0) or 30.0),
            "challenge_cooldown_multiplier": float(getattr(cfg, "challenge_cooldown_multiplier", 2.0) or 2.0),
            "challenge_cooldown_max_seconds": float(getattr(cfg, "challenge_cooldown_max_seconds", 900.0) or 900.0),
            "challenge_cooldown_jitter_ratio": float(getattr(cfg, "challenge_cooldown_jitter_ratio", 0.1) or 0.0),
            "mediamarkt_warm_tabs_enabled": bool(getattr(cfg, "mediamarkt_warm_tabs_enabled", True)),
        }

    @staticmethod
    def _selenium_prewarm_skip_reason(cfg: Any) -> str | None:
        if hasattr(cfg, "selenium_prewarm_skip_reason"):
            try:
                return cfg.selenium_prewarm_skip_reason()
            except Exception:
                pass
        if getattr(cfg, "action_mode", "") != "selenium":
            return "action_mode_not_selenium"
        if not bool(getattr(cfg, "selenium_prewarm_enabled", getattr(cfg, "selenium_prewarm", False))):
            return "prewarm_disabled"
        if not bool(getattr(cfg, "selenium_prewarm_on_runtime_start", True)):
            return "prewarm_on_runtime_start_disabled"
        return None

    @staticmethod
    def _insert_selenium_runtime_log(
        storage: SqliteStorage,
        *,
        level: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            storage.insert_runtime_log(level=level, category="selenium", message=message, details=details or {})
        except Exception:
            logger.exception("[runtime-manager] failed to persist selenium runtime log message=%s", message)

    async def _prewarm_selenium_for_runtime(
        self,
        *,
        cfg: Any,
        storage: SqliteStorage,
        selenium_dispatcher: SeleniumDispatcher,
    ) -> SeleniumWorker | None:
        config_snapshot = self._selenium_config_snapshot(cfg)
        skip_reason = self._selenium_prewarm_skip_reason(cfg)
        if hasattr(selenium_dispatcher, "set_runtime_config"):
            selenium_dispatcher.set_runtime_config(config_snapshot, prewarm_skip_reason=skip_reason)
        self._insert_selenium_runtime_log(
            storage,
            level="INFO",
            message="selenium_config_normalized",
            details=config_snapshot,
        )

        if skip_reason:
            self._insert_selenium_runtime_log(
                storage,
                level="INFO",
                message="selenium_prewarm_skipped",
                details={"reason": skip_reason, "config": config_snapshot},
            )
            return None

        self._insert_selenium_runtime_log(
            storage,
            level="INFO",
            message="selenium_prewarm_requested",
            details={"config": config_snapshot},
        )
        prewarm_result = await asyncio.to_thread(
            selenium_dispatcher.prewarm_browser,
            reason="runtime_start",
            wait_ready_timeout=20,
        )
        selenium_worker = selenium_dispatcher.worker
        if not prewarm_result.get("ok"):
            self._insert_selenium_runtime_log(
                storage,
                level="ERROR" if not prewarm_result.get("skipped") else "INFO",
                message="selenium_prewarm_failed" if not prewarm_result.get("skipped") else "selenium_prewarm_skipped",
                details={"result": prewarm_result, "config": config_snapshot},
            )
            return selenium_worker

        if not bool(getattr(cfg, "watchlist_warm_tabs_enabled", False)):
            self._insert_selenium_runtime_log(
                storage,
                level="INFO",
                message="watchlist_warm_tabs_preload_skipped",
                details={"reason": "warm_tabs_disabled", "config": config_snapshot},
            )
            return selenium_worker

        warm_items = self._select_watchlist_warm_tab_items(storage, cfg)
        if not warm_items:
            self._insert_selenium_runtime_log(
                storage,
                level="INFO",
                message="watchlist_warm_tabs_no_items",
                details={"max": config_snapshot.get("warm_tabs_max", 0)},
            )
            return selenium_worker

        self._insert_selenium_runtime_log(
            storage,
            level="INFO",
            message="watchlist_warm_tabs_preload_started",
            details={"count": len(warm_items), "max": config_snapshot.get("warm_tabs_max", 0)},
        )
        configure_result = selenium_dispatcher.configure_warm_tabs(warm_items)
        preload_result = await asyncio.to_thread(selenium_dispatcher.preload_warm_tabs_now)
        self._insert_selenium_runtime_log(
            storage,
            level="INFO" if preload_result.get("ok") else "WARNING",
            message="watchlist_warm_tabs_preload_finished",
            details={
                "configured": configure_result,
                "preload": preload_result,
                "count": len(warm_items),
            },
        )
        return selenium_worker

    async def _runtime_main(self, *, kind: str, trigger: str) -> None:
        cfg = self.config_manager.load_app_config()
        conn, storage, startup_report = bootstrap_runtime_storage(cfg)
        log_startup_diagnostics(cfg=cfg, project_root=self.paths.repo_root, startup_report=startup_report)
        notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
        antiban = build_antiban(cfg)
        runtime_state = RuntimeStateStore(
            site_labels=SITE_LABELS,
            enabled_map=cfg.parser_enabled_map(),
            action_mode=cfg.action_mode,
            scan_concurrency=cfg.parser_concurrency,
        )
        watchlist_state = WatchlistRuntimeState(cfg=cfg, site_labels=SITE_LABELS)
        selenium_queue: "queue.Queue[Any]" = queue.Queue()

        selenium_dispatcher: SeleniumDispatcher

        def build_selenium_worker() -> SeleniumWorker:
            return SeleniumWorker(
                cfg=cfg,
                job_queue=selenium_queue,
                dispatcher=selenium_dispatcher,
                antiban=antiban,
                storage=storage,
                notifier=notifier,
            )

        selenium_dispatcher = SeleniumDispatcher(
            selenium_queue,
            worker_factory=build_selenium_worker if cfg.action_mode == "selenium" else None,
        )

        pipeline = Pipeline(
            cfg=cfg,
            storage=storage,
            notifier=notifier,
            selenium_dispatcher=selenium_dispatcher,
            antiban=antiban,
            runtime_state=runtime_state,
            watchlist_runtime_state=watchlist_state,
        )
        context = RuntimeExecutionContext(
            cfg=cfg,
            conn=conn,
            storage=storage,
            antiban=antiban,
            runtime_state=runtime_state,
            selenium_queue=selenium_queue,
            selenium_dispatcher=selenium_dispatcher,
            selenium_worker=None,
            pipeline=pipeline,
            watchlist_state=watchlist_state,
            startup_report=startup_report,
        )
        self._register_context(context)

        context.selenium_worker = await self._prewarm_selenium_for_runtime(
            cfg=cfg,
            storage=storage,
            selenium_dispatcher=selenium_dispatcher,
        )

        heartbeat_task = asyncio.create_task(
            heartbeat_loop(
                cfg=cfg,
                antiban=antiban,
                selenium_state=None,
                selenium_dispatcher=selenium_dispatcher,
                runtime_state=runtime_state,
                watchlist_state=watchlist_state,
                storage=storage,
                interval_seconds=cfg.heartbeat_interval_seconds,
            ),
            name=f"heartbeat:{kind}:{trigger}",
        )

        try:
            runtime_state.mark_runtime_started()
            if kind == "once":
                with self._lock:
                    self._timer_last_run_at = utc_now_iso()
                await pipeline.run_once()
                if selenium_dispatcher.worker is not None:
                    await asyncio.to_thread(selenium_queue.join)
            else:
                await pipeline.run_forever()
        finally:
            runtime_state.mark_runtime_stopped("runtime stopped")
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            selenium_dispatcher.stop(timeout=5)
            storage.insert_runtime_log(
                level="INFO",
                category="selenium",
                message="runtime_stop_selenium_closed",
                details=selenium_dispatcher.lifecycle_snapshot(),
            )
            self._record_runtime_snapshot(context)
            conn.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._is_running_locked()
            current_kind = self._current_kind
            started_at = self._last_started_at
            last_exit_code = self._last_exit_code
            last_error = self._last_error
            finished_at = self._last_finished_at
            shutdown_requested = self._close_requested or self._process_exit_scheduled
        return {
            "running": running,
            "pid": os.getpid() if running else None,
            "uptime_seconds": self._uptime_seconds(started_at) if running else 0,
            "started_at": started_at,
            "stopped_at": finished_at,
            "last_exit_code": last_exit_code,
            "last_error": last_error,
            "runtime_kind": current_kind or "stopped",
            "project_root": str(self.paths.repo_root),
            "shutdown_requested": shutdown_requested,
        }

    @staticmethod
    def _build_selenium_snapshot(selenium_state: Any, cfg: Any | None = None) -> dict[str, Any] | None:
        if selenium_state is not None and hasattr(selenium_state, "lifecycle_snapshot"):
            snapshot = selenium_state.lifecycle_snapshot()
            if not snapshot.get("config") and cfg is not None:
                snapshot["config"] = RuntimeManager._selenium_config_snapshot(cfg)
            return snapshot

        config_snapshot = RuntimeManager._selenium_config_snapshot(cfg) if cfg is not None else {}
        skip_reason = RuntimeManager._selenium_prewarm_skip_reason(cfg) if cfg is not None else ""

        if selenium_state is None:
            return {
                "dispatcher_exists": False,
                "dispatcher_running": False,
                "worker_thread_alive": False,
                "lifecycle_state": "stopped",
                "driver_exists": False,
                "driver_session_id": "",
                "browser_started_at": None,
                "last_driver_create_at": None,
                "last_driver_quit_at": None,
                "chromedriver_pid": None,
                "chrome_pid": None,
                "tracked_chrome_pids": [],
                "orphan_app_chrome_pids": [],
                "selenium_window_count": 0,
                "selenium_top_level_window_ids": [],
                "selenium_top_level_window_count": 0,
                "selenium_top_level_window_id_by_handle": {},
                "window_handles_count": 0,
                "window_handles_current_urls": {},
                "last_window_snapshot_at": None,
                "last_start_at": None,
                "last_stop_at": None,
                "last_error": "",
                "started": False,
                "ready": False,
                "busy": False,
                "last_job": "",
                "last_result": "",
                "last_duration_seconds": 0.0,
                "jobs_completed": 0,
                "jobs_failed": 0,
                "jobs_timed_out": 0,
                "driver_rebuilds": 0,
                "last_diagnostic_snapshot": "",
                "config": config_snapshot,
                "prewarmed": False,
                "last_prewarm_skip_reason": skip_reason or "",
                "last_prewarm_error": "",
                "warm_tabs_enabled": False,
                "warm_tabs_count": 0,
                "warm_tabs_max": 0,
                "warm_tabs": [],
                "warm_tab_urls": [],
                "active_action": "",
                "active_worker_action": "",
                "worker_busy": False,
                "warm_refresh_running": False,
                "warm_refresh_paused_reason": "",
                "challenge_detected_count": 0,
                "challenge_sources": {},
                "challenge_manual_action_required": False,
                "last_action_latency_seconds": 0.0,
                "last_button_search_latency_seconds": 0.0,
                "duplicate_start_ignored_count": 0,
                "duplicate_start_guard_count": 0,
            }
        return {
            "dispatcher_exists": True,
            "dispatcher_running": bool(getattr(selenium_state, "started", False)),
            "worker_thread_alive": False,
            "lifecycle_state": getattr(selenium_state, "lifecycle_state", "stopped"),
            "driver_exists": bool(getattr(selenium_state, "driver_session_id", "")),
            "driver_session_id": getattr(selenium_state, "driver_session_id", ""),
            "browser_started_at": getattr(selenium_state, "browser_started_at", None),
            "last_driver_create_at": getattr(selenium_state, "last_driver_create_at", None),
            "last_driver_quit_at": getattr(selenium_state, "last_driver_quit_at", None),
            "chromedriver_pid": getattr(selenium_state, "chromedriver_pid", None),
            "chrome_pid": getattr(selenium_state, "chrome_pid", None),
            "tracked_chrome_pids": list(getattr(selenium_state, "tracked_chrome_pids", []) or []),
            "orphan_app_chrome_pids": list(getattr(selenium_state, "orphan_app_chrome_pids", []) or []),
            "selenium_window_count": getattr(selenium_state, "selenium_window_count", None),
            "selenium_top_level_window_ids": list(getattr(selenium_state, "selenium_top_level_window_ids", []) or []),
            "selenium_top_level_window_count": getattr(selenium_state, "selenium_top_level_window_count", None),
            "selenium_top_level_window_id_by_handle": getattr(
                selenium_state,
                "selenium_top_level_window_id_by_handle",
                {},
            ),
            "window_handles_count": getattr(selenium_state, "window_handles_count", 0),
            "window_handles_current_urls": getattr(selenium_state, "window_handles_current_urls", {}),
            "last_window_snapshot_at": getattr(selenium_state, "last_window_snapshot_at", None),
            "last_start_at": getattr(selenium_state, "last_start_at", None),
            "last_stop_at": getattr(selenium_state, "last_stop_at", None),
            "started": bool(getattr(selenium_state, "started", False)),
            "ready": bool(getattr(selenium_state, "ready", False)),
            "busy": bool(getattr(selenium_state, "busy", False)),
            "last_error": getattr(selenium_state, "last_error", ""),
            "last_job": getattr(selenium_state, "last_job", ""),
            "last_result": getattr(selenium_state, "last_result", ""),
            "last_duration_seconds": getattr(selenium_state, "last_duration_seconds", 0.0),
            "jobs_completed": getattr(selenium_state, "jobs_completed", 0),
            "jobs_failed": getattr(selenium_state, "jobs_failed", 0),
            "jobs_timed_out": getattr(selenium_state, "jobs_timed_out", 0),
            "driver_rebuilds": getattr(selenium_state, "driver_rebuilds", 0),
            "last_diagnostic_snapshot": getattr(selenium_state, "last_diagnostic_snapshot", ""),
            "config": getattr(selenium_state, "config", config_snapshot) or config_snapshot,
            "prewarmed": bool(getattr(selenium_state, "prewarmed", False)),
            "last_prewarm_skip_reason": getattr(selenium_state, "last_prewarm_skip_reason", skip_reason or ""),
            "last_prewarm_error": getattr(selenium_state, "last_prewarm_error", ""),
            "warm_tabs_enabled": bool(getattr(selenium_state, "warm_tabs_enabled", False)),
            "warm_tabs_count": int(getattr(selenium_state, "warm_tabs_count", 0) or 0),
            "warm_tabs_max": int(getattr(selenium_state, "warm_tabs_max", 0) or 0),
            "warm_tabs": getattr(selenium_state, "warm_tabs", []),
            "warm_tab_urls": list(getattr(selenium_state, "warm_tab_urls", []) or []),
            "active_action": getattr(selenium_state, "active_action", ""),
            "active_worker_action": getattr(selenium_state, "active_worker_action", ""),
            "worker_busy": bool(getattr(selenium_state, "busy", False)),
            "warm_refresh_running": bool(getattr(selenium_state, "warm_refresh_running", False)),
            "warm_refresh_paused_reason": getattr(selenium_state, "warm_refresh_paused_reason", ""),
            "challenge_detected_count": int(getattr(selenium_state, "challenge_detected_count", 0) or 0),
            "challenge_sources": dict(getattr(selenium_state, "challenge_sources", {}) or {}),
            "challenge_manual_action_required": bool(
                getattr(selenium_state, "challenge_manual_action_required", False)
            ),
            "last_action_latency_seconds": getattr(selenium_state, "last_action_latency_seconds", 0.0),
            "last_button_search_latency_seconds": getattr(selenium_state, "last_button_search_latency_seconds", 0.0),
            "duplicate_start_ignored_count": getattr(selenium_state, "duplicate_start_ignored_count", 0),
            "duplicate_start_guard_count": getattr(selenium_state, "duplicate_start_guard_count", 0),
        }

    @staticmethod
    def _build_watchlist_lifecycle_snapshot(watchlist: dict[str, Any]) -> dict[str, Any]:
        return {
            "watchlist_running": bool(watchlist.get("running", False)),
            "watchlist_last_cycle": watchlist.get("last_cycle_finished_at") or watchlist.get("last_cycle_started_at"),
            "watchlist_next_cycle": watchlist.get("next_cycle_estimate"),
        }

    def _build_fallback_overview(self, cfg) -> dict[str, Any]:
        placeholder = RuntimeStateStore(
            site_labels=SITE_LABELS,
            enabled_map=cfg.parser_enabled_map(),
            action_mode=cfg.action_mode,
            scan_concurrency=cfg.parser_concurrency,
        ).snapshot_overview(queue_size=0, selenium=RuntimeManager._build_selenium_snapshot(None, cfg))
        last_snapshot = deepcopy(self._last_runtime_snapshot) if self._last_runtime_snapshot else None
        if last_snapshot:
            if "startup_preflight" in last_snapshot:
                placeholder["startup_preflight"] = last_snapshot["startup_preflight"]
            placeholder["warnings"] = list(last_snapshot.get("warnings") or [])
            endpoint_statuses = last_snapshot.get("endpoint_statuses") or self._default_endpoint_statuses()
            self._apply_endpoint_statuses(placeholder, endpoint_statuses)
            for site, state in placeholder["site_states"].items():
                previous = (last_snapshot.get("site_states") or {}).get(site, {})
                state["last_run_at"] = previous.get("last_run_at")
                state["last_success_at"] = previous.get("last_success_at")
                state["last_error_at"] = previous.get("last_error_at")
                state["last_error"] = previous.get("last_error", "")
                state["last_items_found"] = previous.get("last_items_found", 0)
                state["last_events_found"] = previous.get("last_events_found", 0)
                state["runs_started"] = previous.get("runs_started", 0)
                state["runs_completed"] = previous.get("runs_completed", 0)
                state["successes"] = previous.get("successes", 0)
                state["failures"] = previous.get("failures", 0)
                state["skips"] = previous.get("skips", 0)
                if state["enabled"]:
                    state["status"] = "idle"
                    state["message"] = previous.get("message", "")
                else:
                    state["status"] = "disabled"
                    state["message"] = "parser disabled"
        else:
            self._apply_endpoint_statuses(placeholder, self._default_endpoint_statuses())
        return placeholder

    @staticmethod
    def _build_fallback_watchlist_snapshot(cfg, storage=None) -> dict[str, Any]:
        if storage is None:
            try:
                db_path = cfg.resolved_db_path()
                if db_path.exists():
                    with sqlite3.connect(str(db_path), check_same_thread=False) as conn:
                        fallback_storage = SqliteStorage(conn)
                        fallback_storage.init_schema()
                        return WatchlistRuntimeState(cfg=cfg, site_labels=SITE_LABELS).snapshot(storage=fallback_storage)
            except Exception:
                logger.exception("[runtime-manager] failed to build fallback watchlist snapshot")
        return WatchlistRuntimeState(cfg=cfg, site_labels=SITE_LABELS).snapshot(storage=storage)

    def get_timer_status(self) -> dict[str, Any]:
        settings = self.config_manager.get_timer_settings()
        with self._lock:
            next_run_at = self._iso_from_epoch(self._timer_next_run_epoch)
            running = self._is_running_locked()
            runtime_kind = self._current_kind or "stopped"
            last_run_at = self._timer_last_run_at
            last_error = self._timer_last_error
        settings.update(
            {
                "next_run_at": next_run_at,
                "last_run_at": last_run_at,
                "running": running,
                "runtime_kind": runtime_kind,
                "last_error": last_error,
            }
        )
        return settings

    def build_overview(self) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        with self._lock:
            context = self._current_context if self._is_running_locked() else None
            current_kind = self._current_kind or "stopped"

        if context is not None:
            selenium_snapshot = self._build_selenium_snapshot(context.selenium_dispatcher, context.cfg)
            overview = context.runtime_state.snapshot_overview(
                queue_size=context.selenium_dispatcher.counts().get("total", 0),
                selenium=selenium_snapshot,
            )
            self._apply_endpoint_statuses(overview, context.pipeline.endpoint_status_snapshot())
            overview["watchlist"] = context.watchlist_state.snapshot(storage=context.storage)
            overview["watchlist_lifecycle"] = self._build_watchlist_lifecycle_snapshot(overview["watchlist"])
            overview["dispatcher"] = context.selenium_dispatcher.snapshot()
            overview["antiban"] = context.antiban.snapshot()
            overview["startup_preflight"] = context.startup_report.to_dict()
            overview["warnings"] = list(context.startup_report.warnings)
            if selenium_snapshot.get("last_error"):
                overview["warnings"].append(f"Selenium worker error: {selenium_snapshot.get('last_error', '')}")
        else:
            overview = self._build_fallback_overview(cfg)
            overview["watchlist"] = self._build_fallback_watchlist_snapshot(cfg)
            overview["watchlist_lifecycle"] = self._build_watchlist_lifecycle_snapshot(overview["watchlist"])
            overview["dispatcher"] = {"pending": [], "running": []}
            overview["antiban"] = {}

        overview["status"] = self.status()
        overview["timer"] = self.get_timer_status()
        overview["runtime_kind"] = current_kind
        overview["db_path"] = str(cfg.resolved_db_path())
        overview["scan_settings"] = self.config_manager.get_scan_settings_effective()
        overview.setdefault("warnings", [])
        return overview

    def update_action_mode(self, mode: str) -> dict[str, Any]:
        response = self.config_manager.save_action_mode_settings(mode)
        restarted = self.restart_if_running(reason="action mode updated")
        response["runtime_restarted"] = restarted
        return response

    def update_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.config_manager.save_timer_settings(payload)
        with self._lock:
            if response["enabled"]:
                self._timer_next_run_epoch = time.time() + response["interval_seconds"]
            else:
                self._timer_next_run_epoch = None
                self._timer_last_error = ""
        return self.get_timer_status()

    def _timer_loop(self) -> None:
        while not self._timer_stop.wait(1.0):
            try:
                timer_settings = self.config_manager.get_timer_settings()
                if not timer_settings["enabled"]:
                    with self._lock:
                        self._timer_next_run_epoch = None
                    continue

                interval_seconds = max(1, int(timer_settings["interval_seconds"]))
                now = time.time()
                with self._lock:
                    if self._timer_next_run_epoch is None:
                        self._timer_next_run_epoch = now + interval_seconds
                    due = self._timer_next_run_epoch <= now
                    running = self._is_running_locked()
                    current_kind = self._current_kind or "stopped"

                if due and not running:
                    scheduled = self._schedule_runtime(kind="once", trigger="timer")
                    with self._lock:
                        self._timer_next_run_epoch = now + interval_seconds
                        self._timer_last_error = "" if scheduled else "timer run skipped because runtime is already active"
                elif due and running and current_kind == "continuous":
                    with self._lock:
                        self._timer_next_run_epoch = now + interval_seconds
                        self._timer_last_error = "timer paused while continuous runtime is active"
            except Exception as exc:
                logger.exception("[runtime-manager] timer loop failed")
                with self._lock:
                    self._timer_last_error = f"{type(exc).__name__}: {exc}"

    def db_status(self) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        db_path = cfg.resolved_db_path()
        exists = db_path.exists()
        size_bytes = db_path.stat().st_size if exists else 0
        tables: list[str] = []
        if exists:
            with sqlite3.connect(str(db_path), check_same_thread=False) as conn:
                cur = conn.cursor()
                tables = [
                    row[0]
                    for row in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name ASC"
                    ).fetchall()
                ]
        return {
            "path": str(db_path),
            "exists": exists,
            "size_bytes": size_bytes,
            "tables": tables,
        }

    def backup_db(self) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        db_path = cfg.resolved_db_path()
        if not db_path.exists():
            return {
                "ok": False,
                "message": "Database file does not exist yet.",
                "backup_path": None,
            }
        backup_dir = self.paths.app_root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"{db_path.stem}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}{db_path.suffix}"
        backup_sqlite_database(db_path, target)
        return {
            "ok": True,
            "backup_path": str(target),
            "size_bytes": target.stat().st_size,
            "integrity_check": "ok",
        }

    def clear_old_logs(self, *, days: int = 30) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
        with sqlite3.connect(str(cfg.resolved_db_path()), check_same_thread=False) as conn:
            SqliteStorage(conn).init_schema()
            cur = conn.cursor()
            cur.execute("DELETE FROM runtime_logs WHERE created_at < ?", (cutoff_text,))
            removed = cur.rowcount
            conn.commit()
        return {
            "ok": True,
            "removed": removed,
            "cutoff": cutoff_text,
        }

    def clear_stale_actions(self, *, days: int = 30, site: str | None = None) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        normalized_site = (site or "").strip().lower() or None
        if normalized_site and normalized_site not in SITE_LABELS:
            raise ValueError(f"Unknown site: {site}")

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))
        cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
        in_memory_result = {"removed_pending": 0, "removed_jobs": [], "site": normalized_site}
        running_jobs: list[dict[str, Any]] = []

        with self._lock:
            context = self._current_context

        if context is not None:
            in_memory_result = context.selenium_dispatcher.clear_pending(site=normalized_site)
            running_jobs = context.selenium_dispatcher.detailed_snapshot().get("running", [])
            if normalized_site is not None:
                running_jobs = [job for job in running_jobs if job.get("site") == normalized_site]

        with sqlite3.connect(str(cfg.resolved_db_path()), check_same_thread=False) as conn:
            SqliteStorage(conn).init_schema()
            cur = conn.cursor()
            params: list[Any] = [cutoff_text]
            site_clause = ""
            if normalized_site is not None:
                site_clause = " AND site = ?"
                params.append(normalized_site)

            cur.execute(f"DELETE FROM action_log WHERE created_at < ?{site_clause}", params)
            deleted_action_log = cur.rowcount

            lock_params: list[Any] = [cutoff_text]
            lock_site_clause = ""
            if normalized_site is not None:
                lock_site_clause = " AND site = ?"
                lock_params.append(normalized_site)

            skipped_confirmed_purchases = cur.execute(
                f"""
                SELECT COUNT(*)
                FROM purchase_state
                WHERE updated_at < ?{lock_site_clause}
                  AND status = 'purchase_confirmed'
                """,
                lock_params,
            ).fetchone()[0]

            cur.execute(
                f"""
                UPDATE purchase_state
                SET status = 'failed',
                    error_message = 'stale action cleared',
                    updated_at = ?
                WHERE updated_at < ?{lock_site_clause}
                  AND status IN ('queued', 'running')
                """,
                [utc_now_iso(), cutoff_text] + ([] if normalized_site is None else [normalized_site]),
            )
            removed_locks = cur.rowcount

            summary = {
                "event": "stale_action_cleared",
                "site": normalized_site,
                "cutoff": cutoff_text,
                "deleted_pending": in_memory_result["removed_pending"],
                "deleted_action_log": deleted_action_log,
                "reset_running": 0,
                "removed_locks": removed_locks,
                "removed_in_memory_pending": in_memory_result["removed_pending"],
                "running_not_cleared": len(running_jobs),
                "skipped_confirmed_purchases": skipped_confirmed_purchases,
            }
            cur.execute(
                """
                INSERT INTO runtime_logs (level, category, site, message, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "INFO",
                    "action",
                    normalized_site,
                    "stale_action_cleared",
                    json.dumps(summary, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )
            conn.commit()
        return {
            "ok": True,
            **summary,
            "removed_jobs": in_memory_result["removed_jobs"],
            "running_jobs": running_jobs,
        }

    def debug_actions(
        self,
        *,
        site: str | None = None,
        external_id: str | None = None,
        action_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        normalized_site = (site or "").strip().lower() or None
        normalized_external_id = (external_id or "").strip() or None
        normalized_action_id = (action_id or "").strip() or None
        max_rows = max(1, min(500, int(limit)))

        clauses: list[str] = []
        params: list[Any] = []
        if normalized_site is not None:
            clauses.append("site = ?")
            params.append(normalized_site)
        if normalized_external_id is not None:
            clauses.append("external_id = ?")
            params.append(normalized_external_id)
        if normalized_action_id is not None:
            clauses.append("COALESCE(details, '') LIKE ?")
            params.append(f"%{normalized_action_id}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""

        with sqlite3.connect(str(cfg.resolved_db_path()), check_same_thread=False) as conn:
            SqliteStorage(conn).init_schema()
            cur = conn.cursor()
            action_rows = cur.execute(
                f"""
                SELECT id, site, external_id, action_type, action_case, status, details, created_at
                FROM action_log
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params + [max_rows],
            ).fetchall()

            purchase_clauses: list[str] = []
            purchase_params: list[Any] = []
            if normalized_site is not None:
                purchase_clauses.append("site = ?")
                purchase_params.append(normalized_site)
            if normalized_external_id is not None:
                purchase_clauses.append("external_id = ?")
                purchase_params.append(normalized_external_id)
            if normalized_action_id is not None:
                purchase_clauses.append("COALESCE(details_json, '') LIKE ?")
                purchase_params.append(f"%{normalized_action_id}%")
            purchase_where = " WHERE " + " AND ".join(purchase_clauses) if purchase_clauses else ""
            purchase_rows = cur.execute(
                f"""
                SELECT site, purchase_key, external_id, title, product_url, status,
                       created_at, updated_at, last_attempt_at, confirmation_url,
                       confirmation_signal, error_message, details_json
                FROM purchase_state
                {purchase_where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                purchase_params + [max_rows],
            ).fetchall()

            runtime_clauses = ["category IN ('action', 'worker_trace', 'error', 'success')"]
            runtime_params: list[Any] = []
            if normalized_site is not None:
                runtime_clauses.append("site = ?")
                runtime_params.append(normalized_site)
            if normalized_external_id is not None:
                runtime_clauses.append("(message LIKE ? OR COALESCE(details_json, '') LIKE ?)")
                needle = f"%{normalized_external_id}%"
                runtime_params.extend([needle, needle])
            if normalized_action_id is not None:
                runtime_clauses.append("(message LIKE ? OR COALESCE(details_json, '') LIKE ?)")
                needle = f"%{normalized_action_id}%"
                runtime_params.extend([needle, needle])
            runtime_rows = cur.execute(
                f"""
                SELECT id, level, category, site, message, details_json, created_at
                FROM runtime_logs
                WHERE {' AND '.join(runtime_clauses)}
                ORDER BY id DESC
                LIMIT ?
                """,
                runtime_params + [max_rows],
            ).fetchall()

        with self._lock:
            context = self._current_context
        dispatcher = context.selenium_dispatcher.detailed_snapshot() if context is not None else {"pending": [], "running": []}
        if normalized_site is not None:
            dispatcher = {
                key: [job for job in jobs if job.get("site") == normalized_site]
                for key, jobs in dispatcher.items()
            }
        if normalized_external_id is not None:
            dispatcher = {
                key: [job for job in jobs if job.get("external_id") == normalized_external_id]
                for key, jobs in dispatcher.items()
            }
        if normalized_action_id is not None:
            dispatcher = {
                key: [job for job in jobs if job.get("action_id") == normalized_action_id]
                for key, jobs in dispatcher.items()
            }

        def _decode_json(value: str | None) -> Any:
            if not value:
                return None
            try:
                return json.loads(value)
            except Exception:
                return value

        return {
            "ok": True,
            "site": normalized_site,
            "external_id": normalized_external_id,
            "action_id": normalized_action_id,
            "dispatcher": dispatcher,
            "recent_actions": [
                {
                    "id": row[0],
                    "site": row[1],
                    "external_id": row[2],
                    "action_type": row[3],
                    "action_case": row[4],
                    "status": row[5],
                    "details": _decode_json(row[6]),
                    "created_at": row[7],
                }
                for row in action_rows
            ],
            "purchase_locks": [
                {
                    "site": row[0],
                    "purchase_key": row[1],
                    "external_id": row[2],
                    "title": row[3],
                    "product_url": row[4],
                    "status": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                    "last_attempt_at": row[8],
                    "confirmation_url": row[9],
                    "confirmation_signal": row[10],
                    "error_message": row[11],
                    "details": _decode_json(row[12]),
                }
                for row in purchase_rows
            ],
            "recent_action_logs": [
                {
                    "id": row[0],
                    "level": row[1],
                    "category": row[2],
                    "site": row[3],
                    "message": row[4],
                    "details": _decode_json(row[5]),
                    "created_at": row[6],
                }
                for row in runtime_rows
            ],
        }

    def initiate_system_shutdown(self, *, delay_seconds: float = 1.5) -> dict[str, Any]:
        warnings: list[str] = []
        with self._lock:
            had_runtime = self._is_running_locked()
            had_context = self._current_context
            already_scheduled = self._process_exit_scheduled

        self._timer_stop.set()
        with self._lock:
            self._timer_next_run_epoch = None
            self._timer_last_error = ""

        self.stop()

        with self._lock:
            runtime_stopped = not self._is_running_locked()

        selenium_stopped = True
        if had_context is not None:
            selenium_stopped = had_context.selenium_dispatcher.stop(timeout=2)
            if not selenium_stopped:
                warnings.append("Selenium worker is still finishing its shutdown sequence.")

        backend_scheduled = False
        if already_scheduled:
            backend_scheduled = True
        else:
            backend_scheduled = self._schedule_process_exit(delay_seconds=delay_seconds)
            if not backend_scheduled:
                warnings.append("Backend process exit could not be scheduled twice.")

        return {
            "ok": runtime_stopped and selenium_stopped and backend_scheduled,
            "message": "Shutdown started. You can close this browser tab.",
            "stopped": {
                "runtime": runtime_stopped or not had_runtime,
                "scheduler": True,
                "selenium": selenium_stopped,
                "backend": backend_scheduled,
            },
            "warnings": warnings,
        }

    def _schedule_process_exit(self, *, delay_seconds: float) -> bool:
        with self._lock:
            if self._process_exit_scheduled:
                return False
            self._process_exit_scheduled = True

        def _exit_process() -> None:
            try:
                self._shutdown_runtime_manager()
            finally:
                os._exit(0)

        timer = threading.Timer(max(0.5, float(delay_seconds)), _exit_process)
        timer.daemon = True
        timer.start()
        return True

    def _shutdown_runtime_manager(self) -> None:
        with self._lock:
            if self._close_requested:
                return
            self._close_requested = True
            timer_thread = self._timer_thread
            loop_thread = self._loop_thread

        self._timer_stop.set()
        with self._lock:
            self._timer_next_run_epoch = None
            self._timer_last_error = ""
        self.stop()
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass

        if timer_thread.is_alive() and threading.current_thread() is not timer_thread:
            timer_thread.join(timeout=2)
        if loop_thread.is_alive() and threading.current_thread() is not loop_thread:
            loop_thread.join(timeout=2)

    def close(self) -> None:
        self._shutdown_runtime_manager()
