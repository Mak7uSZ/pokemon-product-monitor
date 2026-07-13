from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pokemon_parser.config import AppConfig
from pokemon_parser.engine.access_control import (
    AccessAssessment,
    AccessOutcome,
    AccessSeverity,
    ChallengeDetection,
    ChallengeKind,
    SourceAccessController,
    SourceAccessPolicy,
    SourceAccessState,
    detect_challenge,
)
from pokemon_parser.engine.antiban import AntiBanManager
from pokemon_parser.engine.selenium_dispatcher import SeleniumDispatcher
from pokemon_parser.models import SeleniumJob, SeleniumState
from pokemon_parser.notifications.telegram import TelegramNotifier
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.utils.logging_setup import resolve_debug_log_dir
from pokemon_parser.utils.selenium_diagnostics import (
    LIKELY_CHROME_FAILURE_CAUSES,
    collect_selenium_diagnostics,
    safe_filename_part,
    timestamp_for_filename,
    write_selenium_startup_failure_snapshot,
)
from pokemon_parser.utils.time import utc_now_iso
from pokemon_parser.workers.bol_worker import BolWorkerCase
from pokemon_parser.workers.dreamland_worker import DreamLandWorkerCase
from pokemon_parser.workers.mediamarkt_worker import MediaMarktWorkerCase
from pokemon_parser.workers.observability import (
    WorkerActionTimeline,
    compact_payload,
    detect_mediamarkt_unavailable_markers,
    detect_queue_markers,
    driver_context,
    low_level_debug_enabled,
    safe_current_url,
    safe_title,
    summarize_alerts_toasts,
    summarize_visible_buttons,
)
from pokemon_parser.workers.pocketgames_worker import PocketGamesWorkerCase
from pokemon_parser.workers.purchase_safety import (
    BLOCKING_PURCHASE_STATUSES,
    PURCHASE_STATUS_FAILED,
    PURCHASE_STATUS_PAYMENT_SUBMITTED,
    PURCHASE_STATUS_QUEUED,
    PURCHASE_STATUS_RUNNING,
    PURCHASE_STATUS_UNKNOWN_REVIEW,
    duplicate_skip_status,
    purchase_key_for_target,
)
from pokemon_parser.workers.trace import WorkerTraceLogger

logger = logging.getLogger(__name__)

WORKER_HANDLERS = {
    "bol": BolWorkerCase,
    "pocketgames": PocketGamesWorkerCase,
    "mediamarkt": MediaMarktWorkerCase,
    "dreamland": DreamLandWorkerCase,
}

CONTROLLED_WORKER_RESULTS = {
    "unavailable_at_worker_validation",
    "add_to_cart_button_missing_unavailable",
    "checkout_button_missing_cart_empty",
    "mediamarkt_warm_action_stale_stock",
    "out_of_stock_after_cart",
    "checkout_unavailable",
    "checkout_selector_timeout",
    "queue_detected",
    "bot_blocked",
    "challenge_blocked",
}


class ChallengeBlockedError(RuntimeError):
    pass


@dataclass
class WarmTab:
    site: str
    external_id: str
    product_title: str
    url: str
    window_handle: str = ""
    last_loaded_at: str | None = None
    last_refreshed_at: str | None = None
    last_loaded_epoch: float = 0.0
    last_refreshed_epoch: float = 0.0
    last_refresh_duration_ms: float = 0.0
    last_dom_status: str = "unknown"
    last_error: str = ""
    warm_state: str = "loading"
    retry_after_epoch: float = 0.0
    access_state: str = "normal"
    challenge_type: str = ""
    challenge_confidence: str = "none"
    challenge_reason_code: str = ""
    challenge_signals: tuple[str, ...] = ()
    challenge_detected_at: str | None = None
    manual_action_required: bool = False


class SeleniumWorker(threading.Thread):
    _active_lock = threading.Lock()
    _active_worker: "SeleniumWorker | None" = None

    def __init__(
        self,
        cfg: AppConfig,
        job_queue: "queue.Queue[SeleniumJob]",
        dispatcher: SeleniumDispatcher | None = None,
        antiban: AntiBanManager | None = None,
        storage: SqliteStorage | None = None,
        notifier: TelegramNotifier | None = None,
    ):
        super().__init__(daemon=False, name="selenium-worker")
        self.cfg = cfg
        self.job_queue = job_queue
        self.dispatcher = dispatcher
        self.antiban = antiban
        self.storage = storage
        self.notifier = notifier

        self.driver = None
        self.state = SeleniumState()
        self.ready_event = threading.Event()
        self.stop_event = threading.Event()

        self._driver_lock = threading.RLock()
        self._start_guard_lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._warm_tabs_lock = threading.RLock()
        self._start_requested = False
        self._claimed_active_worker = False
        self._warm_targets: dict[tuple[str, str], WarmTab] = {}
        self._warm_tabs: dict[tuple[str, str], WarmTab] = {}
        self._challenge_access = SourceAccessController()
        self._last_warm_refresh_epoch = 0.0
        self._last_warm_refresh_skipped_action_log_epoch = 0.0
        self._last_mediamarkt_warm_switch_monotonic = 0.0
        self._last_mediamarkt_hot_path_started_monotonic = 0.0
        self._last_mediamarkt_warm_tab_state: dict[str, object] = {}
        self._app_user_data_dir = self._normalized_path_text(getattr(cfg, "chrome_user_data_dir", ""))
        self._app_debug_log_dir = self._normalized_path_text(str(resolve_debug_log_dir(getattr(cfg, "base_dir", None))))
        self._app_process_marker = "--superparser-app-owned=selenium"
        self.state.lifecycle_state = "stopped"
        self.state.config = self._selenium_config_snapshot()
        self.state.warm_tabs_enabled = bool(getattr(cfg, "watchlist_warm_tabs_enabled", False))
        self.state.warm_tabs_max = int(getattr(cfg, "watchlist_warm_tabs_max", 0) or 0)

    def _log_runtime(
        self,
        level: str,
        category: str,
        message: str,
        *,
        site: str | None = None,
        details: dict | None = None,
    ) -> None:
        log_method = getattr(logger, level.lower(), logger.info)
        prefix = f"[{category}]"
        if site:
            prefix += f"[{site}]"
        log_method("%s %s", prefix, message)

        if self.storage is not None:
            try:
                self.storage.insert_runtime_log(
                    level=level,
                    category=category,
                    message=message,
                    site=site,
                    details=details,
                )
            except Exception:
                logger.exception("[selenium] failed to persist runtime log category=%s site=%s", category, site)

    def _selenium_config_snapshot(self) -> dict:
        if hasattr(self.cfg, "selenium_runtime_config"):
            try:
                return dict(self.cfg.selenium_runtime_config())
            except Exception:
                pass
        return {
            "action_mode": getattr(self.cfg, "action_mode", ""),
            "legacy_prewarm": bool(getattr(self.cfg, "selenium_prewarm", False)),
            "prewarm_enabled": bool(
                getattr(self.cfg, "selenium_prewarm_enabled", getattr(self.cfg, "selenium_prewarm", False))
            ),
            "prewarm_on_runtime_start": bool(getattr(self.cfg, "selenium_prewarm_on_runtime_start", True)),
            "keep_browser_alive": bool(getattr(self.cfg, "selenium_keep_browser_alive", True)),
            "warm_tabs_enabled": bool(getattr(self.cfg, "watchlist_warm_tabs_enabled", False)),
            "warm_tabs_max": int(getattr(self.cfg, "watchlist_warm_tabs_max", 0) or 0),
            "challenge_cooldown_base_seconds": float(getattr(self.cfg, "challenge_cooldown_base_seconds", 30.0) or 30.0),
            "challenge_cooldown_multiplier": float(getattr(self.cfg, "challenge_cooldown_multiplier", 2.0) or 2.0),
            "challenge_cooldown_max_seconds": float(getattr(self.cfg, "challenge_cooldown_max_seconds", 900.0) or 900.0),
            "challenge_cooldown_jitter_ratio": float(getattr(self.cfg, "challenge_cooldown_jitter_ratio", 0.1) or 0.0),
            "mediamarkt_warm_tabs_enabled": bool(getattr(self.cfg, "mediamarkt_warm_tabs_enabled", True)),
        }

    def _prewarm_skip_reason(self) -> str | None:
        if hasattr(self.cfg, "selenium_prewarm_skip_reason"):
            try:
                return self.cfg.selenium_prewarm_skip_reason()
            except Exception:
                pass
        if getattr(self.cfg, "action_mode", "") != "selenium":
            return "action_mode_not_selenium"
        if not bool(getattr(self.cfg, "selenium_prewarm_enabled", getattr(self.cfg, "selenium_prewarm", False))):
            return "prewarm_disabled"
        if not bool(getattr(self.cfg, "selenium_prewarm_on_runtime_start", True)):
            return "prewarm_on_runtime_start_disabled"
        return None

    def _caller_stack(self, *, limit: int = 8) -> list[str]:
        stack = traceback.format_stack(limit=limit + 2)[:-2]
        return [line.strip() for line in stack[-limit:]]

    def _diagnostic_base(self, *, function: str, extra: dict | None = None) -> dict:
        worker_busy = bool(self.state.busy)
        dispatcher_running = False
        if self.dispatcher is not None:
            try:
                worker = getattr(self.dispatcher, "worker", None)
                dispatcher_running = bool(worker is self or (worker is not None and getattr(worker, "is_alive", lambda: False)()))
            except Exception:
                dispatcher_running = False
        payload = {
            "function": function,
            "caller_stack": self._caller_stack(),
            "current_process_pid": os.getpid(),
            "runtime_running": bool(self.state.started),
            "dispatcher_running": dispatcher_running,
            "worker_running": bool(self.is_alive()) if hasattr(self, "is_alive") else False,
            "worker_busy": worker_busy,
            "action_active": bool(self.state.active_worker_action or self.state.active_action),
            "driver_exists": self.driver is not None,
            "active_worker_action": self.state.active_worker_action or self.state.active_action,
            "chromedriver_pid": self.state.chromedriver_pid,
            "chrome_pid": self.state.chrome_pid,
            "tracked_chrome_pids": list(getattr(self.state, "tracked_chrome_pids", []) or []),
            "orphan_app_chrome_pids": list(getattr(self.state, "orphan_app_chrome_pids", []) or []),
            "window_handles_count": int(getattr(self.state, "window_handles_count", 0) or 0),
            "window_handles_current_urls": dict(getattr(self.state, "window_handles_current_urls", {}) or {}),
            "selenium_top_level_window_ids": list(getattr(self.state, "selenium_top_level_window_ids", []) or []),
            "selenium_top_level_window_count": getattr(self.state, "selenium_top_level_window_count", None),
            "last_window_snapshot_at": getattr(self.state, "last_window_snapshot_at", None),
        }
        if extra:
            payload.update(extra)
        return payload

    def _log_diagnostic(self, message: str, *, function: str, level: str = "INFO", extra: dict | None = None) -> None:
        self._log_runtime(level, "selenium", message, details=self._diagnostic_base(function=function, extra=extra))

    def _record_duplicate_start_guard(self) -> None:
        self.state.duplicate_start_ignored_count += 1
        if hasattr(self.state, "duplicate_start_guard_count"):
            self.state.duplicate_start_guard_count += 1

    def start(self) -> bool:
        with self._start_guard_lock:
            if self._start_requested or self.is_alive():
                self._record_duplicate_start_guard()
                logger.info("selenium_worker_start_ignored_already_running")
                self._log_runtime("INFO", "selenium", "selenium_worker_start_ignored_already_running")
                return False

            with self._active_lock:
                active = type(self)._active_worker
                if active is not None and active is not self and active.is_alive():
                    self.state.last_error = "another selenium worker is already active"
                    self._record_duplicate_start_guard()
                    logger.info("selenium_worker_start_ignored_already_running")
                    self._log_runtime(
                        "WARNING",
                        "selenium",
                        "selenium_worker_start_ignored_already_running",
                        details={"active_thread": getattr(active, "name", "selenium-worker")},
                    )
                    return False
                type(self)._active_worker = self
                self._claimed_active_worker = True

            self._start_requested = True
            self.state.started = True
            self.state.lifecycle_state = "starting"
            self.state.last_start_at = utc_now_iso()
            try:
                super().start()
            except Exception:
                self._release_active_worker()
                self._start_requested = False
                raise
            return True

    def _release_active_worker(self) -> None:
        if not self._claimed_active_worker:
            return
        with self._active_lock:
            if type(self)._active_worker is self:
                type(self)._active_worker = None
        self._claimed_active_worker = False

    def _safe_driver_url(self) -> str:
        try:
            return self.driver.current_url if self.driver is not None else ""
        except Exception:
            return ""

    @staticmethod
    def _safe_driver_session_id(driver) -> str:
        try:
            return str(getattr(driver, "session_id", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _driver_process_ids(driver) -> tuple[int | None, int | None]:
        chromedriver_pid: int | None = None
        chrome_pid: int | None = None
        try:
            service = getattr(driver, "service", None)
            process = getattr(service, "process", None)
            pid = getattr(process, "pid", None)
            chromedriver_pid = int(pid) if pid is not None else None
        except Exception:
            chromedriver_pid = None

        try:
            capabilities = getattr(driver, "capabilities", {}) or {}
            pid = capabilities.get("goog:processID") or capabilities.get("browserProcessId")
            chrome_pid = int(pid) if pid is not None else None
        except Exception:
            chrome_pid = None

        return chromedriver_pid, chrome_pid

    @staticmethod
    def _selenium_window_count_locked(driver) -> int | None:
        try:
            handles = list(getattr(driver, "window_handles", []) or [])
        except Exception:
            return None
        if not handles:
            return 0
        if not hasattr(driver, "execute_cdp_cmd"):
            return None
        try:
            current_handle = getattr(driver, "current_window_handle", None)
        except Exception:
            current_handle = None
        window_ids: set[int] = set()
        try:
            for handle in handles:
                driver.switch_to.window(handle)
                result = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
                window_id = result.get("windowId") if isinstance(result, dict) else None
                if window_id is not None:
                    window_ids.add(int(window_id))
        except Exception:
            return None
        finally:
            if current_handle:
                try:
                    driver.switch_to.window(current_handle)
                except Exception:
                    pass
        return len(window_ids) if window_ids else None

    def selenium_window_count(self) -> int | None:
        if self.driver is None:
            return 0
        if self.state.busy:
            return None
        if not self._action_lock.acquire(blocking=False):
            return None
        try:
            with self._driver_lock:
                self.state.selenium_window_count = self._selenium_window_count_locked(self.driver)
                return self.state.selenium_window_count
        finally:
            self._action_lock.release()

    def _record_driver_started(self, driver) -> None:
        chromedriver_pid, chrome_pid = self._driver_process_ids(driver)
        self.state.driver_session_id = self._safe_driver_session_id(driver)
        self.state.browser_started_at = utc_now_iso()
        self.state.last_driver_create_at = self.state.browser_started_at
        self.state.chromedriver_pid = chromedriver_pid
        self.state.chrome_pid = chrome_pid
        self._snapshot_window_handles_locked(reason="driver_started", log=True, driver=driver)
        self._refresh_process_tracking(reason="driver_started", log_detected=True)
        self._write_app_driver_marker()
        self._log_runtime(
            "INFO",
            "selenium",
            "selenium_driver_started",
            details={
                "driver_session_id": self.state.driver_session_id,
                "browser_started_at": self.state.browser_started_at,
                "last_driver_create_at": self.state.last_driver_create_at,
                "chromedriver_pid": chromedriver_pid,
                "chrome_pid": chrome_pid,
                "selenium_window_count": self.state.selenium_window_count,
            },
        )

    def _clear_driver_metadata(self) -> None:
        self.state.driver_session_id = ""
        self.state.browser_started_at = None
        self.state.chromedriver_pid = None
        self.state.chrome_pid = None
        self.state.tracked_chrome_pids = []
        self.state.orphan_app_chrome_pids = []
        self.state.selenium_window_count = 0
        self.state.selenium_top_level_window_ids = []
        self.state.selenium_top_level_window_count = 0
        self.state.selenium_top_level_window_id_by_handle = {}
        self.state.window_handles_count = 0
        self.state.window_handles_current_urls = {}
        self.state.last_window_snapshot_at = None

    def close_driver(self, *, reason: str) -> None:
        with self._driver_lock:
            driver = self.driver
            if driver is None:
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "runtime_stop_started",
                    details={"reason": reason, "driver_exists": False},
                )
                self._refresh_process_tracking(reason=f"close_driver_no_driver:{reason}", log_detected=True)
                if self.state.orphan_app_chrome_pids:
                    self._log_runtime(
                        "WARNING",
                        "selenium",
                        "runtime_stop_orphan_chrome_detected",
                        details={"reason": reason, "orphan_app_chrome_pids": self.state.orphan_app_chrome_pids},
                    )
                    self._log_runtime(
                        "WARNING",
                        "selenium",
                        "runtime_stop_orphan_app_chrome_detected",
                        details={"reason": reason, "orphan_app_chrome_pids": self.state.orphan_app_chrome_pids},
                    )
                    self._log_runtime(
                        "WARNING",
                        "selenium",
                        "selenium_orphan_app_chrome_detected",
                        details={"reason": reason, "orphan_app_chrome_pids": self.state.orphan_app_chrome_pids},
                    )
                    self._terminate_app_owned_chrome_pids(
                        reason=f"close_driver_no_driver:{reason}",
                        pids=self.state.orphan_app_chrome_pids,
                    )
                self._clear_driver_metadata()
                self.state.last_stop_at = utc_now_iso()
                self.state.last_driver_quit_at = self.state.last_stop_at
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "selenium_runtime_stop_finished",
                    details={"reason": reason, "driver_exists": False},
                )
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "runtime_stop_finished",
                    details={"reason": reason, "driver_exists": False},
                )
                return

            details = {
                "reason": reason,
                "driver_session_id": self.state.driver_session_id or self._safe_driver_session_id(driver),
                "chromedriver_pid": self.state.chromedriver_pid,
                "chrome_pid": self.state.chrome_pid,
            }
            chromedriver_pid = self.state.chromedriver_pid
            tracked_chrome_pids = list(getattr(self.state, "tracked_chrome_pids", []) or [])
            stop_finished_logged = False
            self.state.warm_refresh_paused_reason = "stop_requested"
            self._log_runtime(
                "INFO",
                "selenium",
                "selenium_runtime_stop_started",
                details=details,
            )
            self._log_runtime("INFO", "selenium", "runtime_stop_started", details=details)
            self._log_runtime(
                "INFO",
                "selenium",
                "selenium_warm_refresh_stop_requested",
                details={"reason": reason, "warm_refresh_running": self.state.warm_refresh_running},
            )
            self._refresh_process_tracking(reason=f"before_driver_quit:{reason}", log_detected=True)
            tracked_chrome_pids = sorted(set(tracked_chrome_pids) | set(self.state.tracked_chrome_pids))
            self._log_runtime("INFO", "selenium", "selenium_driver_quit_started", details=details)
            try:
                driver.quit()
            except Exception as exc:
                self.state.last_error = f"driver_quit_failed: {type(exc).__name__}: {exc}"
                self._log_runtime(
                    "ERROR",
                    "selenium",
                    "selenium_driver_quit_failed",
                    details={**details, "error_type": type(exc).__name__, "error": str(exc)},
                )
                service = getattr(driver, "service", None)
                if service is not None and hasattr(service, "stop"):
                    try:
                        service.stop()
                    except Exception as service_exc:
                        self._log_runtime(
                            "ERROR",
                            "selenium",
                            "selenium_driver_service_stop_failed",
                            details={
                                **details,
                                "error_type": type(service_exc).__name__,
                                "error": str(service_exc),
                            },
                        )
                self._terminate_app_owned_chrome_pids(reason=f"driver_quit_failed:{reason}", pids=tracked_chrome_pids)
            else:
                chromedriver_gone = self._verify_chromedriver_stopped(chromedriver_pid)
                self._refresh_process_tracking(reason=f"after_driver_quit:{reason}", log_detected=True)
                remaining_tracked = sorted(set(tracked_chrome_pids) | set(self.state.tracked_chrome_pids))
                remaining_tracked = [pid for pid in remaining_tracked if self._process_exists(pid)]
                orphan_results = []
                if remaining_tracked:
                    self._log_runtime(
                        "WARNING",
                        "selenium",
                        "runtime_stop_orphan_chrome_detected",
                        details={"reason": reason, "orphan_app_chrome_pids": remaining_tracked},
                    )
                    self._log_runtime(
                        "WARNING",
                        "selenium",
                        "runtime_stop_orphan_app_chrome_detected",
                        details={"reason": reason, "orphan_app_chrome_pids": remaining_tracked},
                    )
                    self._log_runtime(
                        "WARNING",
                        "selenium",
                        "selenium_orphan_app_chrome_detected",
                        details={"reason": reason, "orphan_app_chrome_pids": remaining_tracked},
                    )
                    orphan_results = self._terminate_app_owned_chrome_pids(
                        reason=f"after_driver_quit:{reason}",
                        pids=remaining_tracked,
                    )
                self.state.last_driver_quit_at = utc_now_iso()
                self._log_runtime(
                    "INFO" if chromedriver_gone else "WARNING",
                    "selenium",
                    "selenium_driver_quit_finished",
                    details={
                        **details,
                        "last_driver_quit_at": self.state.last_driver_quit_at,
                        "chromedriver_gone": chromedriver_gone,
                        "tracked_chrome_pids": tracked_chrome_pids,
                        "orphan_cleanup": orphan_results,
                    },
                )
                if chromedriver_gone:
                    self._clear_app_driver_marker()
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "selenium_runtime_stop_finished",
                    details={
                        "reason": reason,
                        "chromedriver_gone": chromedriver_gone,
                        "remaining_tracked_chrome_pids": [
                            pid for pid in tracked_chrome_pids if self._process_exists(pid)
                        ],
                    },
                )
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "runtime_stop_finished",
                    details={"reason": reason, "chromedriver_gone": chromedriver_gone},
                )
                stop_finished_logged = True
            finally:
                self.driver = None
                with self._warm_tabs_lock:
                    for tab in self._warm_tabs.values():
                        tab.window_handle = ""
                        tab.warm_state = "stale"
                self._clear_driver_metadata()
                self.state.last_stop_at = utc_now_iso()
                if self.state.last_driver_quit_at is None:
                    self.state.last_driver_quit_at = self.state.last_stop_at
                if not stop_finished_logged:
                    self._log_runtime(
                        "WARNING",
                        "selenium",
                        "selenium_runtime_stop_finished",
                        details={
                            "reason": reason,
                            "last_error": self.state.last_error,
                            "last_driver_quit_at": self.state.last_driver_quit_at,
                        },
                    )

    def _debug_log_dir(self) -> Path:
        return resolve_debug_log_dir(getattr(self.cfg, "base_dir", None))

    def _chromedriver_log_path(self) -> Path:
        log_dir = self._debug_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / f"chromedriver_{timestamp_for_filename()}.log"

    def _app_driver_marker_path(self) -> Path:
        log_dir = self._debug_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / "selenium_app_driver.json"

    @staticmethod
    def _normalized_path_text(value: str | None) -> str:
        if not value:
            return ""
        try:
            return str(Path(value).expanduser().resolve()).lower()
        except Exception:
            return str(value).strip().lower()

    def _profile_marker_details(self) -> dict:
        return {
            "owner": "pokemon_parser_selenium",
            "chrome_user_data_dir": self._normalized_path_text(getattr(self.cfg, "chrome_user_data_dir", "")),
            "chrome_profile_dir": str(getattr(self.cfg, "chrome_profile_dir", "") or "").strip(),
            "app_process_marker": self._app_process_marker,
        }

    def _read_app_driver_marker(self) -> dict:
        try:
            marker_path = self._app_driver_marker_path()
            if not marker_path.exists():
                return {}
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            logger.exception("[selenium] failed to read app driver marker")
            return {}

    def _write_app_driver_marker(self) -> None:
        payload = {
            **self._profile_marker_details(),
            "created_at": self.state.last_driver_create_at or self.state.browser_started_at,
            "driver_session_id": self.state.driver_session_id,
            "chromedriver_pid": self.state.chromedriver_pid,
            "chrome_pid": self.state.chrome_pid,
            "tracked_chrome_pids": list(getattr(self.state, "tracked_chrome_pids", []) or []),
            "chromedriver_alive_at_create": bool(self.state.chromedriver_pid),
        }
        try:
            self._app_driver_marker_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("[selenium] failed to write app driver marker")

    def _clear_app_driver_marker(self) -> None:
        try:
            self._app_driver_marker_path().unlink(missing_ok=True)
        except Exception:
            logger.exception("[selenium] failed to clear app driver marker")

    @staticmethod
    def _process_exists(pid: int | None) -> bool:
        if not pid or int(pid) <= 0:
            return False
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except Exception:
                return False
            return f'"{int(pid)}"' in (completed.stdout or "")
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _list_processes() -> list[dict]:
        if os.name == "nt":
            command = (
                "Get-CimInstance Win32_Process | "
                "Select-Object Name,ProcessId,ParentProcessId,CommandLine | "
                "ConvertTo-Json -Depth 3 -Compress"
            )
            try:
                completed = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", command],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except Exception:
                return []
            text = (completed.stdout or "").strip()
            if not text:
                return []
            try:
                payload = json.loads(text)
            except Exception:
                return []
            if isinstance(payload, dict):
                payload = [payload]
            if not isinstance(payload, list):
                return []
            return [
                {
                    "name": str(item.get("Name") or ""),
                    "pid": int(item.get("ProcessId") or 0),
                    "ppid": int(item.get("ParentProcessId") or 0),
                    "command_line": str(item.get("CommandLine") or ""),
                }
                for item in payload
                if isinstance(item, dict) and item.get("ProcessId")
            ]

        try:
            completed = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,comm=,args="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return []
        processes: list[dict] = []
        for line in (completed.stdout or "").splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            processes.append(
                {
                    "name": parts[2],
                    "pid": pid,
                    "ppid": ppid,
                    "command_line": parts[3] if len(parts) > 3 else parts[2],
                }
            )
        return processes

    @staticmethod
    def _is_chrome_process(process: dict) -> bool:
        name = str(process.get("name") or "").lower()
        return name in {"chrome.exe", "chrome", "google-chrome", "chromium", "chromium-browser"}

    @staticmethod
    def _is_chromedriver_process(process: dict) -> bool:
        name = str(process.get("name") or "").lower()
        return name in {"chromedriver.exe", "chromedriver"}

    def _command_uses_app_profile(self, command_line: str) -> bool:
        if not self._app_user_data_dir:
            return False
        return self._app_user_data_dir in str(command_line or "").lower()

    def _command_has_app_marker(self, command_line: str) -> bool:
        return bool(self._app_process_marker and self._app_process_marker.lower() in str(command_line or "").lower())

    def _command_uses_app_debug_log_dir(self, command_line: str) -> bool:
        if not self._app_debug_log_dir:
            return False
        return self._app_debug_log_dir in str(command_line or "").lower()

    @staticmethod
    def _child_map(processes: list[dict]) -> dict[int, list[int]]:
        children: dict[int, list[int]] = {}
        for process in processes:
            try:
                children.setdefault(int(process.get("ppid") or 0), []).append(int(process.get("pid") or 0))
            except Exception:
                continue
        return children

    @classmethod
    def _descendant_pids(cls, processes: list[dict], root_pids: set[int]) -> set[int]:
        children = cls._child_map(processes)
        seen: set[int] = set()
        pending = [pid for pid in root_pids if pid]
        while pending:
            pid = pending.pop()
            for child_pid in children.get(pid, []):
                if child_pid and child_pid not in seen:
                    seen.add(child_pid)
                    pending.append(child_pid)
        return seen

    def _marker_app_pids(self) -> set[int]:
        marker = self._read_app_driver_marker()
        if not marker or not self._marker_matches_current_profile(marker):
            return set()
        pids: set[int] = set()
        for key in ("chromedriver_pid", "chrome_pid"):
            try:
                pid = int(marker.get(key) or 0)
            except Exception:
                pid = 0
            if pid > 0:
                pids.add(pid)
        for pid in marker.get("tracked_chrome_pids") or []:
            try:
                pid = int(pid or 0)
            except Exception:
                pid = 0
            if pid > 0:
                pids.add(pid)
        return pids

    def _refresh_process_tracking(self, *, reason: str, log_detected: bool = False) -> dict:
        processes = self._list_processes()
        chromedriver_pid = int(self.state.chromedriver_pid or 0)
        chrome_pid = int(self.state.chrome_pid or 0)
        chrome_processes = [process for process in processes if self._is_chrome_process(process)]
        chromedriver_processes = [process for process in processes if self._is_chromedriver_process(process)]

        chromedriver_children = {
            int(process.get("pid") or 0)
            for process in chrome_processes
            if chromedriver_pid and int(process.get("ppid") or 0) == chromedriver_pid
        }
        current_roots = ({chrome_pid} if chrome_pid else set()) | chromedriver_children
        current_related = set(current_roots) | self._descendant_pids(processes, current_roots)
        app_profile_pids = {
            int(process.get("pid") or 0)
            for process in chrome_processes
            if self._command_uses_app_profile(str(process.get("command_line") or ""))
        }
        app_marker_pids = {
            int(process.get("pid") or 0)
            for process in chrome_processes
            if self._command_has_app_marker(str(process.get("command_line") or ""))
        }
        marker_pids = self._marker_app_pids()
        marker_chrome_pids = {
            int(process.get("pid") or 0)
            for process in chrome_processes
            if int(process.get("pid") or 0) in marker_pids
        }
        app_owned_chrome_pids = app_profile_pids | app_marker_pids | marker_chrome_pids
        tracked_chrome_pids = sorted(pid for pid in (app_owned_chrome_pids | current_related) if pid)
        orphan_app_chrome_pids = sorted(pid for pid in (app_owned_chrome_pids - current_related) if pid)

        self.state.tracked_chrome_pids = tracked_chrome_pids
        self.state.orphan_app_chrome_pids = orphan_app_chrome_pids
        if not self.state.chrome_pid and tracked_chrome_pids:
            self.state.chrome_pid = tracked_chrome_pids[0]

        snapshot = {
            "reason": reason,
            "chromedriver_pid": self.state.chromedriver_pid,
            "chrome_pid": self.state.chrome_pid,
            "tracked_chrome_pids": tracked_chrome_pids,
            "orphan_app_chrome_pids": orphan_app_chrome_pids,
            "chrome_processes": [
                {
                    "pid": process.get("pid"),
                    "ppid": process.get("ppid"),
                    "command_line": process.get("command_line"),
                    "app_profile": self._command_uses_app_profile(str(process.get("command_line") or "")),
                    "app_marker": self._command_has_app_marker(str(process.get("command_line") or "")),
                }
                for process in chrome_processes
                if int(process.get("pid") or 0) in set(tracked_chrome_pids) | set(orphan_app_chrome_pids)
            ],
            "chromedriver_processes": [
                {
                    "pid": process.get("pid"),
                    "ppid": process.get("ppid"),
                    "command_line": process.get("command_line"),
                    "current_driver": int(process.get("pid") or 0) == chromedriver_pid,
                }
                for process in chromedriver_processes
                if int(process.get("pid") or 0) == chromedriver_pid
            ],
        }
        if log_detected:
            self._log_diagnostic("chrome_process_detected", function="_refresh_process_tracking", extra=snapshot)
        return snapshot

    @staticmethod
    def _process_command_line(pid: int | None) -> str:
        if not pid or int(pid) <= 0:
            return ""
        proc_cmdline = Path(f"/proc/{int(pid)}/cmdline")
        if proc_cmdline.exists():
            try:
                return proc_cmdline.read_text(encoding="utf-8", errors="ignore").replace("\x00", " ")
            except Exception:
                return ""
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    [
                        "wmic",
                        "process",
                        "where",
                        f"ProcessId={int(pid)}",
                        "get",
                        "CommandLine",
                        "/value",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except Exception:
                completed = None
            output = (completed.stdout or "").strip() if completed is not None else ""
            if output:
                return output
            try:
                fallback = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except Exception:
                return ""
            return (fallback.stdout or "").strip()
        return ""

    def _marker_matches_current_profile(self, marker: dict) -> bool:
        if marker.get("owner") != "pokemon_parser_selenium":
            return False
        current = self._profile_marker_details()
        return (
            marker.get("chrome_user_data_dir") == current["chrome_user_data_dir"]
            and str(marker.get("chrome_profile_dir") or "") == current["chrome_profile_dir"]
        )

    @staticmethod
    def _command_contains_all(command_line: str, needles: list[str]) -> bool:
        lowered = command_line.lower()
        return bool(lowered) and all(needle.lower() in lowered for needle in needles if needle)

    def _terminate_marked_process(self, *, pid: int | None, name: str, expected_command_parts: list[str]) -> dict:
        pid = int(pid or 0)
        if pid <= 0 or not self._process_exists(pid):
            return {"pid": pid or None, "name": name, "terminated": False, "reason": "not_running"}
        command_line = self._process_command_line(pid)
        if command_line and not self._command_contains_all(command_line, expected_command_parts):
            return {"pid": pid, "name": name, "terminated": False, "reason": "command_mismatch"}
        if not command_line and expected_command_parts:
            return {"pid": pid, "name": name, "terminated": False, "reason": "command_unverified"}
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except Exception as exc:
                return {
                    "pid": pid,
                    "name": name,
                    "terminated": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if not self._process_exists(pid):
                    return {"pid": pid, "name": name, "terminated": True, "reason": "terminated"}
                time.sleep(0.1)
            return {"pid": pid, "name": name, "terminated": False, "reason": "still_running"}
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            return {
                "pid": pid,
                "name": name,
                "terminated": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not self._process_exists(pid):
                return {"pid": pid, "name": name, "terminated": True, "reason": "terminated"}
            time.sleep(0.1)
        return {"pid": pid, "name": name, "terminated": False, "reason": "still_running"}

    def _terminate_app_owned_chrome_pids(self, *, reason: str, pids: list[int] | None = None) -> list[dict]:
        self._refresh_process_tracking(reason=f"{reason}_before_terminate", log_detected=True)
        candidate_pids = sorted(set(pids or self.state.tracked_chrome_pids or self.state.orphan_app_chrome_pids or []))
        results: list[dict] = []
        for pid in candidate_pids:
            command_line = self._process_command_line(pid)
            has_profile = self._command_uses_app_profile(command_line)
            has_marker = self._command_has_app_marker(command_line)
            app_owned = (
                has_profile
                or has_marker
                or pid in self._marker_app_pids()
            )
            if not app_owned:
                results.append({"pid": pid, "terminated": False, "reason": "not_app_owned"})
                continue
            expected_parts = []
            if has_marker:
                expected_parts.append(self._app_process_marker)
            if has_profile:
                expected_parts.append(self._app_user_data_dir)
            if not expected_parts:
                results.append({"pid": pid, "terminated": False, "reason": "command_unverified"})
                continue
            result = self._terminate_marked_process(
                pid=pid,
                name="chrome",
                expected_command_parts=expected_parts,
            )
            results.append(result)
        terminated = [result.get("pid") for result in results if result.get("terminated")]
        if terminated:
            self._log_runtime(
                "WARNING",
                "selenium",
                "selenium_orphan_app_chrome_terminated",
                details={"reason": reason, "terminated_pids": terminated, "results": results},
            )
        self._refresh_process_tracking(reason=f"{reason}_after_terminate", log_detected=True)
        return results

    def _close_old_app_driver_processes(self, *, reason: str) -> bool:
        marker = self._read_app_driver_marker()
        if not marker or not self._marker_matches_current_profile(marker):
            self._refresh_process_tracking(reason=reason, log_detected=True)
            processes = self._list_processes()
            old_app_chromedriver_pids = sorted(
                int(process.get("pid") or 0)
                for process in processes
                if self._is_chromedriver_process(process)
                and self._command_uses_app_debug_log_dir(str(process.get("command_line") or ""))
            )
            old_driver_results = []
            for pid in old_app_chromedriver_pids:
                old_driver_results.append(
                    self._terminate_marked_process(
                        pid=pid,
                        name="chromedriver",
                        expected_command_parts=[self._app_debug_log_dir],
                    )
                )
            old_driver_closed = any(result.get("terminated") for result in old_driver_results)
            if old_driver_closed:
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "selenium_old_app_driver_closed",
                    details={
                        "reason": reason,
                        "old_app_chromedriver_pids": old_app_chromedriver_pids,
                        "results": old_driver_results,
                    },
                )
            if self.state.orphan_app_chrome_pids:
                self._log_runtime(
                    "WARNING",
                    "selenium",
                    "runtime_stop_orphan_chrome_detected",
                    details={
                        "reason": reason,
                        "orphan_app_chrome_pids": self.state.orphan_app_chrome_pids,
                    },
                )
                self._log_runtime(
                    "WARNING",
                    "selenium",
                    "runtime_stop_orphan_app_chrome_detected",
                    details={
                        "reason": reason,
                        "orphan_app_chrome_pids": self.state.orphan_app_chrome_pids,
                    },
                )
                self._log_runtime(
                    "WARNING",
                    "selenium",
                    "selenium_orphan_app_chrome_detected",
                    details={
                        "reason": reason,
                        "orphan_app_chrome_pids": self.state.orphan_app_chrome_pids,
                    },
                )
                results = self._terminate_app_owned_chrome_pids(
                    reason=reason,
                    pids=self.state.orphan_app_chrome_pids,
                )
                return old_driver_closed or any(result.get("terminated") for result in results)
            return old_driver_closed

        if reason == "profile_lock_detected":
            self._log_runtime(
                "WARNING",
                "selenium",
                "selenium_profile_lock_detected",
                details={"marker": marker},
            )

        user_data_dir = str(marker.get("chrome_user_data_dir") or "")
        marker_chrome_pids: list[int] = []
        for pid in marker.get("tracked_chrome_pids") or []:
            try:
                chrome_pid = int(pid or 0)
            except Exception:
                chrome_pid = 0
            if chrome_pid > 0:
                marker_chrome_pids.append(chrome_pid)
        try:
            primary_chrome_pid = int(marker.get("chrome_pid") or 0)
        except Exception:
            primary_chrome_pid = 0
        if primary_chrome_pid > 0:
            marker_chrome_pids.append(primary_chrome_pid)
        marker_chrome_pids = sorted(set(marker_chrome_pids))
        try:
            marker_chromedriver_pid = int(marker.get("chromedriver_pid") or 0)
        except Exception:
            marker_chromedriver_pid = 0
        results = [
            self._terminate_marked_process(
                pid=marker_chromedriver_pid,
                name="chromedriver",
                expected_command_parts=["chromedriver"],
            )
        ]
        if user_data_dir and marker_chrome_pids:
            results.extend(
                self._terminate_app_owned_chrome_pids(
                    reason=f"old_app_driver:{reason}",
                    pids=marker_chrome_pids,
                )
            )
        closed = any(result.get("terminated") for result in results)
        any_alive = any(
            self._process_exists(pid)
            for pid in [marker_chromedriver_pid, *marker_chrome_pids]
            if pid
        )
        if closed:
            self._log_runtime(
                "INFO",
                "selenium",
                "selenium_old_app_driver_closed",
                details={"reason": reason, "marker": marker, "results": results},
            )
        if not any_alive:
            self._clear_app_driver_marker()
        return closed

    def _verify_chromedriver_stopped(self, chromedriver_pid: int | None) -> bool:
        if not chromedriver_pid:
            return True
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not self._process_exists(chromedriver_pid):
                return True
            time.sleep(0.1)
        return not self._process_exists(chromedriver_pid)

    def _dump_failure_artifacts(
        self,
        job: SeleniumJob,
        trace: WorkerTraceLogger | None,
        *,
        timeline: WorkerActionTimeline | None = None,
        total_duration_seconds: float | None = None,
    ) -> dict[str, str]:
        if self.driver is None:
            return {}

        artifact_dir = self._debug_log_dir() / "worker_failures"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        action_id = job.action_id or (trace.action_id if trace is not None else "")
        prefix = "_".join(
            [
                timestamp_for_filename(),
                safe_filename_part(job.site),
                safe_filename_part(job.target.external_id),
                safe_filename_part(action_id),
            ]
        )
        metadata_path = artifact_dir / f"{prefix}.txt"
        paths: dict[str, str] = {}
        timeline_path = ""

        if timeline is not None:
            try:
                timeline_path = timeline.write(result="failure", reason=self.state.last_error or "worker_failed")
                paths["timeline"] = timeline_path
            except Exception as exc:
                paths["timeline_error"] = f"{type(exc).__name__}: {exc}"

        try:
            final_url = safe_current_url(self.driver)
            final_title = safe_title(self.driver)
            visible_buttons = summarize_visible_buttons(self.driver, limit=30)
            visible_alerts = summarize_alerts_toasts(self.driver)
            unavailable_markers = (
                detect_mediamarkt_unavailable_markers(self.driver)
                if job.site == "mediamarkt"
                else {"found": False, "markers": [], "disabled_checkout_buttons": []}
            )
            queue_markers = detect_queue_markers(self.driver, job.site)
            active_lock_state = {
                "worker_busy": self.state.busy,
                "active_action": self.state.active_action,
                "active_worker_action": self.state.active_worker_action,
                "action_lock_locked": self._action_lock.locked() if hasattr(self._action_lock, "locked") else None,
                "warm_refresh_running": self.state.warm_refresh_running,
                "warm_refresh_paused_reason": self.state.warm_refresh_paused_reason,
            }
            timeline_events = timeline.events[-30:] if timeline is not None else []
            availability_decision = {}
            if timeline is not None:
                for event in reversed(timeline.events):
                    decision = event.get("availability_decision")
                    if isinstance(decision, dict) and decision:
                        availability_decision = decision
                        break
            metadata = {
                "site": job.site,
                "external_id": job.target.external_id,
                "title": job.target.title,
                "action_id": action_id,
                "case": job.case,
                "final_url": final_url,
                "final_title": final_title,
                "url": final_url,
                "metadata": compact_payload(dict(job.metadata)),
                "created_at": job.created_at,
                "current_step": timeline.current_step if timeline is not None else "",
                "last_successful_step": timeline.last_successful_step if timeline is not None else "",
                "timed_out_selector": timeline.timed_out_selector if timeline is not None else "",
                "timed_out_condition": timeline.timed_out_selector if timeline is not None else "",
                "timeout_seconds": timeline.timeout_seconds if timeline is not None else None,
                "total_duration_seconds": round(total_duration_seconds, 3) if total_duration_seconds is not None else None,
                "total_job_received_to_click_ms": (
                    timeline.total_job_received_to_click_ms if timeline is not None else None
                ),
                "total_job_received_to_checkout_ms": (
                    timeline.total_job_received_to_checkout_ms if timeline is not None else None
                ),
                "per_step_timings": dict(timeline.timings) if timeline is not None else {},
                "last_30_low_level_events": timeline_events,
                "current_visible_buttons_summary": visible_buttons,
                "current_visible_alert_toast_snackbar_summary": visible_alerts,
                "unavailable_markers_found": unavailable_markers,
                "availability_decision": availability_decision,
                "queue_markers_found": queue_markers,
                "warm_tab_state": self._last_mediamarkt_warm_tab_state if job.site == "mediamarkt" else {},
                "active_worker_action_lock_state": active_lock_state,
                "sensitive_artifacts_omitted": True,
                "timeline_path": timeline_path,
            }
            metadata_path.write_text(
                json.dumps(compact_payload(metadata), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            paths["metadata"] = str(metadata_path)
        except Exception as exc:
            paths["metadata_error"] = f"{type(exc).__name__}: {exc}"

        return paths

    @staticmethod
    def _queued_for_seconds(created_at: str) -> float | None:
        if not created_at:
            return None
        try:
            normalized = created_at[:-1] + "+00:00" if created_at.endswith("Z") else created_at
            created = datetime.fromisoformat(normalized)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return round(max(0.0, (datetime.now(timezone.utc) - created).total_seconds()), 2)
        except Exception:
            return None

    def _prepare_pocketgames_purchase(self, job: SeleniumJob, trace: WorkerTraceLogger) -> bool:
        if job.site != "pocketgames" or self.storage is None:
            return True

        purchase_key = purchase_key_for_target(job.target)
        blocking = set(BLOCKING_PURCHASE_STATUSES)
        blocking.discard(PURCHASE_STATUS_QUEUED)
        reserved, existing = self.storage.reserve_purchase_state(
            site=job.site,
            purchase_key=purchase_key,
            external_id=job.target.external_id,
            title=job.target.title,
            product_url=job.target.product_url,
            status=PURCHASE_STATUS_RUNNING,
            blocking_statuses=blocking,
            details={"source": "selenium_worker_start"},
        )
        if reserved:
            trace.step(
                "PocketGames duplicate check passed",
                {"phase": "duplicate_check", "purchase_key": purchase_key},
            )
            return True

        existing_status = existing["status"] if existing else "unknown"
        skip_status = duplicate_skip_status(existing_status)
        trace.warning(
            "PocketGames duplicate skipped",
            {
                "phase": "duplicate_check",
                "purchase_key": purchase_key,
                "existing_status": existing_status,
                "status": skip_status,
            },
            level="minimal",
        )
        trace.set_result(
            skip_status,
            {
                "purchase_key": purchase_key,
                "existing_status": existing_status,
            },
        )
        self.storage.insert_runtime_log(
            level="WARNING",
            category="action",
            site=job.site,
            message=f"pocketgames duplicate skipped status={skip_status} existing_status={existing_status}",
            details={
                "purchase_key": purchase_key,
                "external_id": job.target.external_id,
                "existing_status": existing_status,
                "status": skip_status,
            },
        )
        return False

    def _record_pocketgames_worker_failure(self, job: SeleniumJob, exc: Exception | None, *, timed_out: bool) -> None:
        if job.site != "pocketgames" or self.storage is None:
            return

        purchase_key = purchase_key_for_target(job.target)
        current = self.storage.get_purchase_state(job.site, purchase_key)
        current_status = current["status"] if current else ""
        status = PURCHASE_STATUS_UNKNOWN_REVIEW if current_status == PURCHASE_STATUS_PAYMENT_SUBMITTED else PURCHASE_STATUS_FAILED
        reason = "job timed out" if timed_out else f"{type(exc).__name__}: {exc}" if exc is not None else "worker failed"
        self.storage.update_purchase_state(
            site=job.site,
            purchase_key=purchase_key,
            status=status,
            external_id=job.target.external_id,
            title=job.target.title,
            product_url=job.target.product_url,
            error_message=reason,
            details={
                "previous_status": current_status,
                "timed_out": timed_out,
            },
        )

    def _pocketgames_purchase_status(self, job: SeleniumJob) -> str | None:
        if job.site != "pocketgames" or self.storage is None:
            return None
        state = self.storage.get_purchase_state(job.site, purchase_key_for_target(job.target))
        if state is None:
            return None
        return state.get("status")

    def _driver_is_healthy(self) -> bool:
        if self.driver is None:
            return False
        try:
            _ = self.driver.current_window_handle
            return True
        except Exception:
            return False

    def ensure_driver(self):
        with self._driver_lock:
            if self._driver_is_healthy():
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "selenium_driver_create_skipped_existing_driver",
                    details={
                        "driver_session_id": self.state.driver_session_id,
                        "chromedriver_pid": self.state.chromedriver_pid,
                        "chrome_pid": self.state.chrome_pid,
                    },
                )
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "selenium_existing_driver_reused",
                    details={"driver_session_id": self.state.driver_session_id},
                )
                return self.driver
            if self.driver is not None:
                self.close_driver(reason="unhealthy_driver")
            self._close_old_app_driver_processes(reason="before_driver_create")
            if self._driver_is_healthy():
                self._log_runtime("INFO", "selenium", "selenium_driver_create_skipped_existing_driver")
                return self.driver
            self._log_diagnostic("selenium_driver_create_requested", function="ensure_driver")
            self._log_runtime("INFO", "selenium", "selenium_driver_create_started")
            self.driver = self.init_driver()
            self.state.driver_rebuilds += 1
            self._log_diagnostic("selenium_driver_created", function="ensure_driver")
            self._log_runtime(
                "INFO",
                "selenium",
                "selenium_driver_create_finished",
                details={
                    "driver_session_id": self.state.driver_session_id,
                    "browser_started_at": self.state.browser_started_at,
                    "last_driver_create_at": self.state.last_driver_create_at,
                    "chromedriver_pid": self.state.chromedriver_pid,
                    "chrome_pid": self.state.chrome_pid,
                    "selenium_window_count": self.state.selenium_window_count,
                },
            )
            return self.driver

    def prewarm_browser(self, *, reason: str = "runtime_start") -> dict[str, bool | str]:
        self.state.config = self._selenium_config_snapshot()
        skip_reason = self._prewarm_skip_reason()
        if skip_reason:
            self.state.last_prewarm_skip_reason = skip_reason
            self._log_runtime(
                "INFO",
                "selenium",
                "selenium_prewarm_skipped",
                details={"reason": skip_reason, "config": self.state.config, "prewarm_reason": reason},
            )
            return {"ok": False, "skipped": True, "reason": skip_reason}

        with self._action_lock:
            if self._driver_is_healthy():
                self.state.prewarmed = True
                self.state.ready = True
                self.state.last_prewarm_skip_reason = "driver_already_exists"
                self.state.last_prewarm_error = ""
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "selenium_prewarm_skipped",
                    details={"reason": "driver_already_exists", "config": self.state.config, "prewarm_reason": reason},
                )
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "selenium_existing_driver_reused",
                    details={"driver_session_id": self.state.driver_session_id, "prewarm_reason": reason},
                )
                return {"ok": True, "skipped": True, "reason": "driver_already_exists"}

            self.state.last_prewarm_skip_reason = ""
            self.state.last_prewarm_error = ""
            self._log_runtime(
                "INFO",
                "selenium",
                "selenium_prewarm_started",
                details={"config": self.state.config, "prewarm_reason": reason},
            )
            try:
                self.ensure_driver()
            except Exception as exc:
                self.state.prewarmed = False
                self.state.ready = False
                self.state.last_prewarm_error = f"{type(exc).__name__}: {exc}"
                self.state.last_error = f"selenium_prewarm_failed: {type(exc).__name__}: {exc}"
                self._log_runtime(
                    "ERROR",
                    "selenium",
                    "selenium_prewarm_failed",
                    details={
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "config": self.state.config,
                        "prewarm_reason": reason,
                    },
                )
                return {"ok": False, "skipped": False, "reason": "selenium_prewarm_failed"}

            self.state.prewarmed = True
            self.state.ready = True
            self.state.last_prewarm_error = ""
            self._log_runtime(
                "INFO",
                "selenium",
                "selenium_prewarm_ready",
                details={
                    "driver_session_id": self.state.driver_session_id,
                    "browser_started_at": self.state.browser_started_at,
                    "chromedriver_pid": self.state.chromedriver_pid,
                    "chrome_pid": self.state.chrome_pid,
                    "config": self.state.config,
                    "prewarm_reason": reason,
                },
            )
            return {"ok": True, "skipped": False, "reason": ""}

    @staticmethod
    def _warm_key(site: str, external_id: str) -> tuple[str, str]:
        return (str(site or "").strip().lower(), str(external_id or "").strip())

    def _warm_tabs_allowed(self) -> bool:
        return bool(
            getattr(self.cfg, "watchlist_warm_tabs_enabled", False)
            and getattr(self.cfg, "selenium_keep_browser_alive", True)
            and getattr(self.cfg, "action_mode", "selenium") == "selenium"
        )

    @staticmethod
    def _challenge_tab_source(tab: WarmTab) -> str:
        return f"browser.tab.{tab.site}.{tab.external_id}"

    @staticmethod
    def _challenge_site_source(site: str) -> str:
        return f"browser.source.{site}"

    def _challenge_policy(self) -> SourceAccessPolicy:
        return SourceAccessPolicy(
            soft_escalation_threshold=2,
            observation_window_seconds=300.0,
            base_cooldown_seconds=float(getattr(self.cfg, "challenge_cooldown_base_seconds", 30.0) or 30.0),
            cooldown_multiplier=float(getattr(self.cfg, "challenge_cooldown_multiplier", 2.0) or 2.0),
            max_cooldown_seconds=float(getattr(self.cfg, "challenge_cooldown_max_seconds", 900.0) or 900.0),
            jitter_ratio=float(getattr(self.cfg, "challenge_cooldown_jitter_ratio", 0.1) or 0.0),
        )

    def _detect_driver_challenge(self, driver, *, source: str) -> ChallengeDetection:
        snapshot: dict = {}
        try:
            result = driver.execute_script(
                """
                const challengeSignalSnapshot = true;
                const selectorSignals = [];
                const selectors = [
                  ['recaptcha_iframe', 'iframe[src*="recaptcha" i]'],
                  ['hcaptcha_iframe', 'iframe[src*="hcaptcha" i]'],
                  ['captcha_dom', '.g-recaptcha,.h-captcha,[id*="captcha" i],[class*="captcha" i]'],
                  ['challenge_form', 'form[action*="/challenge" i],form#challenge-form,[data-testid*="challenge-form" i]'],
                  ['cloudflare_interstitial', '#cf-challenge-running,#cf-chl-widget,[class^="cf-chl-"]']
                ];
                for (const [name, selector] of selectors) {
                  try { if (document.querySelector(selector)) selectorSignals.push(name); } catch (_) {}
                }
                const headings = Array.from(document.querySelectorAll('h1,h2,[role="heading"]'))
                  .slice(0, 5).map((node) => (node.innerText || node.textContent || '').trim()).join(' ');
                return {
                  title: (document.title || '').slice(0, 300),
                  url: (location.href || '').slice(0, 2000),
                  headings: headings.slice(0, 1500),
                  bodyText: ((document.body && document.body.innerText) || '').slice(0, 6000),
                  selectorSignals
                };
                """
            )
            if isinstance(result, dict):
                snapshot = result
        except Exception:
            snapshot = {}

        title = str(snapshot.get("title") or safe_title(driver) or "")
        url = str(snapshot.get("url") or safe_current_url(driver) or "")
        text = " ".join(
            str(value or "")
            for value in (snapshot.get("headings"), snapshot.get("bodyText"))
        )
        selector_signals = tuple(str(value) for value in (snapshot.get("selectorSignals") or []) if value)
        synthetic_html = " ".join(
            {
                "recaptcha_iframe": "g-recaptcha",
                "hcaptcha_iframe": "h-captcha",
                "captcha_dom": "captcha-container",
                "challenge_form": "challenge-platform",
                "cloudflare_interstitial": "cf-chl-",
            }.get(signal, "")
            for signal in selector_signals
        )
        detection = detect_challenge(
            url=url,
            title=title,
            text=text,
            html=synthetic_html,
            source=source,
        )
        if detection.detected and selector_signals:
            detection = ChallengeDetection(
                detected=True,
                kind=detection.kind,
                reason_code=detection.reason_code,
                confidence="high",
                evidence=tuple(dict.fromkeys((*detection.evidence, *selector_signals))),
                source=detection.source,
                recommended_action=detection.recommended_action,
            )
        return detection

    @staticmethod
    def _challenge_assessment(detection: ChallengeDetection) -> AccessAssessment:
        if detection.kind == ChallengeKind.RATE_LIMITED:
            return AccessAssessment(
                AccessOutcome.TRANSIENT_FAILURE,
                detection.reason_code or "challenge_rate_limited",
                AccessSeverity.DEGRADED,
                status_code=429,
                retryable=True,
                challenge=detection,
            )
        return AccessAssessment(
            AccessOutcome.STRONG_DENY,
            detection.reason_code or "challenge_unknown",
            AccessSeverity.STRONG,
            status_code=403,
            challenge=detection,
        )

    @staticmethod
    def _tab_is_quarantined(tab: WarmTab) -> bool:
        return tab.warm_state in {"challenged", "quarantined", "cooling_down", "probing", "manually_blocked"}

    def _refresh_challenge_state_snapshot(self) -> None:
        with self._warm_tabs_lock:
            tabs = list(self._warm_tabs.values())
        sources: dict[str, dict] = {}
        for site in sorted({tab.site for tab in tabs}):
            site_tabs = [tab for tab in tabs if tab.site == site]
            snapshot = self._challenge_access.snapshot(self._challenge_site_source(site))
            snapshot.update(
                {
                    "site": site,
                    "tabs_total": len(site_tabs),
                    "tabs_challenged": sum(1 for tab in site_tabs if self._tab_is_quarantined(tab)),
                    "manual_action_required": any(tab.manual_action_required for tab in site_tabs),
                }
            )
            sources[site] = snapshot
        self.state.challenge_sources = sources
        self.state.challenge_manual_action_required = any(
            bool(snapshot.get("manual_action_required")) for snapshot in sources.values()
        )

    def _quarantine_challenged_tab(
        self,
        tab: WarmTab,
        detection: ChallengeDetection,
        *,
        context: str,
    ) -> None:
        assessment = self._challenge_assessment(detection)
        policy = self._challenge_policy()
        tab_decision = self._challenge_access.observe(
            self._challenge_tab_source(tab),
            assessment,
            policy,
        )
        tab.warm_state = "quarantined"
        tab.access_state = (
            SourceAccessState.MANUALLY_BLOCKED.value
            if detection.kind == ChallengeKind.CAPTCHA
            else tab_decision.state.value
        )
        tab.challenge_type = detection.kind.value if detection.kind else ChallengeKind.UNKNOWN_CHALLENGE.value
        tab.challenge_confidence = detection.confidence
        tab.challenge_reason_code = detection.reason_code or "challenge_unknown"
        tab.challenge_signals = detection.evidence
        tab.challenge_detected_at = utc_now_iso()
        tab.manual_action_required = detection.kind == ChallengeKind.CAPTCHA
        tab.last_dom_status = "challenge"
        tab.last_error = tab.challenge_reason_code
        tab.retry_after_epoch = max(tab.retry_after_epoch, tab_decision.cooldown_until)
        self.state.challenge_detected_count += 1

        with self._warm_tabs_lock:
            site_tabs = [candidate for candidate in self._warm_tabs.values() if candidate.site == tab.site]
        all_site_tabs_challenged = bool(site_tabs) and all(self._tab_is_quarantined(candidate) for candidate in site_tabs)
        site_decision = None
        if all_site_tabs_challenged:
            site_decision = self._challenge_access.observe(
                self._challenge_site_source(tab.site),
                assessment,
                policy,
            )
            tab.retry_after_epoch = max(tab.retry_after_epoch, site_decision.cooldown_until)

        details = {
            "external_id": tab.external_id,
            "context": context,
            "challenge_type": tab.challenge_type,
            "confidence": tab.challenge_confidence,
            "reason_code": tab.challenge_reason_code,
            "matched_signals": list(tab.challenge_signals),
            "tab_state": tab.access_state,
            "tab_cooldown_seconds": round(tab_decision.cooldown_seconds, 3),
            "source_paused": bool(site_decision),
            "source_state": site_decision.state.value if site_decision else "normal",
            "source_cooldown_seconds": round(site_decision.cooldown_seconds, 3) if site_decision else 0.0,
            "manual_action_required": tab.manual_action_required,
            "browser_restart_requested": False,
        }
        detected_state = (
            SourceAccessState.SUSPECTED_CHALLENGE.value
            if detection.kind == ChallengeKind.RATE_LIMITED
            else SourceAccessState.CHALLENGED.value
        )
        self._log_runtime(
            "WARNING",
            "challenge",
            "browser_challenge_detected",
            site=tab.site,
            details={**details, "state_transition": f"normal->{detected_state}"},
        )
        self._log_runtime(
            "WARNING",
            "challenge",
            "browser_challenge_quarantined",
            site=tab.site,
            details={**details, "state_transition": f"{detected_state}->{tab.access_state}"},
        )
        self._refresh_challenge_state_snapshot()

    def _record_normal_tab_load(self, tab: WarmTab, *, context: str) -> None:
        policy = self._challenge_policy()
        success = AccessAssessment(AccessOutcome.SUCCESS, "browser_normal_page")
        had_challenge = bool(tab.challenge_reason_code)
        tab_decision = self._challenge_access.observe(self._challenge_tab_source(tab), success, policy)
        site_snapshot = self._challenge_access.snapshot(self._challenge_site_source(tab.site))
        if site_snapshot.get("state") != "normal":
            self._challenge_access.observe(self._challenge_site_source(tab.site), success, policy)
        tab.access_state = tab_decision.state.value
        tab.challenge_type = ""
        tab.challenge_confidence = "none"
        tab.challenge_reason_code = ""
        tab.challenge_signals = ()
        tab.challenge_detected_at = None
        tab.manual_action_required = False
        tab.retry_after_epoch = 0.0
        if had_challenge:
            self._log_runtime(
                "INFO",
                "challenge",
                "browser_challenge_recovered",
                site=tab.site,
                details={
                    "external_id": tab.external_id,
                    "context": context,
                    "state_transition": "probing->recovered",
                    "reason_code": "browser_normal_page",
                },
            )
        self._refresh_challenge_state_snapshot()

    def _source_load_allowed(self, tab: WarmTab) -> bool:
        allowed, reason = self._challenge_access.allow(self._challenge_site_source(tab.site))
        if not allowed:
            site_snapshot = self._challenge_access.snapshot(self._challenge_site_source(tab.site))
            cooldown_until = float(site_snapshot.get("cooldown_until_epoch") or 0.0)
            tab.retry_after_epoch = max(tab.retry_after_epoch, cooldown_until)
            tab.access_state = "cooling_down"
            self._refresh_challenge_state_snapshot()
            return False
        if reason == "probe" and self._tab_is_quarantined(tab):
            tab.warm_state = "probing"
            tab.access_state = "probing"
            self._log_runtime(
                "INFO",
                "challenge",
                "browser_challenge_probe_started",
                site=tab.site,
                details={"external_id": tab.external_id, "state_transition": "cooling_down->probing"},
            )
        return True

    def _block_action_for_challenge(
        self,
        *,
        job: SeleniumJob,
        tab: WarmTab,
        trace: WorkerTraceLogger,
        reason_code: str,
    ) -> None:
        details = {
            "external_id": job.target.external_id,
            "reason_code": reason_code,
            "challenge_type": tab.challenge_type or "source_cooldown",
            "challenge_confidence": tab.challenge_confidence,
            "matched_signals": list(tab.challenge_signals),
            "manual_action_required": tab.manual_action_required,
            "action_executed": False,
        }
        trace.set_result("challenge_blocked", details)
        self._log_runtime(
            "WARNING",
            "action",
            "browser_challenge_action_blocked",
            site=job.site,
            details=details,
        )
        raise ChallengeBlockedError(f"challenge_blocked:{reason_code}")

    def configure_warm_tabs(self, items: list[dict]) -> dict[str, int | bool]:
        if not self._warm_tabs_allowed():
            with self._warm_tabs_lock:
                self._warm_targets.clear()
            self.state.warm_tabs_enabled = False
            self.state.warm_tabs_count = 0
            return {"ok": True, "configured": 0}
        if self.state.busy or self.state.active_worker_action or self.state.active_action:
            self._log_warm_refresh_skipped_action_running(reason="configure_warm_tabs")
            with self._warm_tabs_lock:
                configured = len(self._warm_tabs)
            return {"ok": False, "configured": configured, "reason": "action_active"}

        max_tabs = max(1, int(getattr(self.cfg, "watchlist_warm_tabs_max", 6) or 6))
        selected: dict[tuple[str, str], WarmTab] = {}
        for item in items:
            if len(selected) >= max_tabs:
                break
            if not bool(item.get("enabled", True)):
                continue
            site = str(item.get("site") or "").strip().lower()
            if site == "mediamarkt" and not bool(getattr(self.cfg, "mediamarkt_warm_tabs_enabled", True)):
                continue
            high_priority = site == "mediamarkt" or bool(item.get("pinned")) or bool(item.get("matched_filter_ids"))
            if not high_priority:
                continue
            url = str(item.get("url") or "").strip()
            external_id = str(item.get("article_number") or item.get("product_key") or item.get("sku") or "").strip()
            if not site or not url or not external_id:
                continue
            key = self._warm_key(site, external_id)
            selected[key] = WarmTab(
                site=site,
                external_id=external_id,
                product_title=str(item.get("title") or external_id),
                url=url,
                warm_state="loading",
            )

        with self._warm_tabs_lock:
            previous = self._warm_tabs
            self._warm_targets = selected
            self._warm_tabs = {}
            for key, target in selected.items():
                existing = previous.get(key)
                if existing is not None:
                    target.window_handle = existing.window_handle
                    target.last_loaded_at = existing.last_loaded_at
                    target.last_refreshed_at = existing.last_refreshed_at
                    target.last_loaded_epoch = existing.last_loaded_epoch
                    target.last_refreshed_epoch = existing.last_refreshed_epoch
                    target.last_refresh_duration_ms = existing.last_refresh_duration_ms
                    target.last_dom_status = existing.last_dom_status
                    target.last_error = existing.last_error
                    target.warm_state = existing.warm_state
                    target.retry_after_epoch = existing.retry_after_epoch
                    target.access_state = existing.access_state
                    target.challenge_type = existing.challenge_type
                    target.challenge_confidence = existing.challenge_confidence
                    target.challenge_reason_code = existing.challenge_reason_code
                    target.challenge_signals = existing.challenge_signals
                    target.challenge_detected_at = existing.challenge_detected_at
                    target.manual_action_required = existing.manual_action_required
                self._warm_tabs[key] = target

        self.state.warm_tabs_enabled = True
        self.state.warm_tabs_max = max_tabs
        self.state.warm_tabs_count = len(selected)
        self._refresh_challenge_state_snapshot()
        self._log_runtime(
            "INFO",
            "selenium",
            "watchlist_warm_tabs_configured",
            details={"configured": len(selected), "max": max_tabs},
        )
        return {"ok": True, "configured": len(selected)}

    def _driver_handles(self) -> set[str]:
        try:
            return set(self.driver.window_handles if self.driver is not None else [])
        except Exception:
            return set()

    def _top_level_window_id_for_handle(self, driver, handle: str) -> int | None:
        if driver is None or not handle or not hasattr(driver, "execute_cdp_cmd"):
            return None
        try:
            driver.switch_to.window(handle)
            result = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
            if isinstance(result, dict) and result.get("windowId") is not None:
                return int(result["windowId"])
        except Exception:
            return None
        return None

    def _snapshot_window_handles_locked(self, *, reason: str, log: bool = False, driver=None) -> dict:
        driver = driver or self.driver
        if driver is None:
            self.state.window_handles_count = 0
            self.state.window_handles_current_urls = {}
            self.state.selenium_window_count = 0
            self.state.selenium_top_level_window_ids = []
            self.state.selenium_top_level_window_count = 0
            self.state.selenium_top_level_window_id_by_handle = {}
            self.state.last_window_snapshot_at = utc_now_iso()
            return {
                "reason": reason,
                "window_handles_count": 0,
                "window_handles_current_urls": {},
                "selenium_window_count": 0,
                "selenium_top_level_window_ids": [],
                "selenium_top_level_window_count": 0,
                "selenium_top_level_window_id_by_handle": {},
            }

        try:
            handles = list(driver.window_handles or [])
        except Exception as exc:
            snapshot = {
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}",
                "window_handles_count": 0,
                "window_handles_current_urls": {},
                "selenium_window_count": None,
                "selenium_top_level_window_ids": [],
                "selenium_top_level_window_count": None,
                "selenium_top_level_window_id_by_handle": {},
            }
            if log:
                self._log_diagnostic("selenium_window_handles_snapshot", function="_snapshot_window_handles_locked", extra=snapshot)
            return snapshot

        try:
            original_handle = driver.current_window_handle
        except Exception:
            original_handle = None

        urls: dict[str, str] = {}
        top_window_ids: list[int] = []
        top_window_id_by_handle: dict[str, int] = {}
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                urls[handle] = str(getattr(driver, "current_url", "") or "")
                if hasattr(driver, "execute_cdp_cmd"):
                    result = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
                    if isinstance(result, dict) and result.get("windowId") is not None:
                        window_id = int(result["windowId"])
                        top_window_ids.append(window_id)
                        top_window_id_by_handle[handle] = window_id
            except Exception as exc:
                urls[handle] = f"<error:{type(exc).__name__}:{exc}>"
        if original_handle:
            try:
                driver.switch_to.window(original_handle)
            except Exception:
                pass

        unique_top_windows = sorted(set(top_window_ids))
        self.state.window_handles_count = len(handles)
        self.state.window_handles_current_urls = urls
        self.state.selenium_window_count = len(unique_top_windows) if unique_top_windows else None
        self.state.selenium_top_level_window_ids = unique_top_windows
        self.state.selenium_top_level_window_count = self.state.selenium_window_count
        self.state.selenium_top_level_window_id_by_handle = top_window_id_by_handle
        self.state.last_window_snapshot_at = utc_now_iso()
        snapshot = {
            "reason": reason,
            "window_handles_count": len(handles),
            "window_handles_current_urls": urls,
            "selenium_window_count": self.state.selenium_window_count,
            "selenium_top_level_window_ids": unique_top_windows,
            "selenium_top_level_window_count": self.state.selenium_top_level_window_count,
            "selenium_top_level_window_id_by_handle": top_window_id_by_handle,
            "last_window_snapshot_at": self.state.last_window_snapshot_at,
        }
        if log:
            self._log_diagnostic("selenium_window_handles_snapshot", function="_snapshot_window_handles_locked", extra=snapshot)
            if self.state.selenium_window_count is not None:
                self._log_diagnostic("selenium_top_level_window_snapshot", function="_snapshot_window_handles_locked", extra=snapshot)
                self._log_diagnostic("selenium_top_window_detected", function="_snapshot_window_handles_locked", extra=snapshot)
        return snapshot

    def _reuse_initial_handle_for_warm_tab(self, driver, tab: WarmTab) -> bool:
        with self._warm_tabs_lock:
            existing_handles = {
                existing.window_handle
                for existing in self._warm_tabs.values()
                if existing.window_handle
            }
        if existing_handles:
            return False
        try:
            handles = list(driver.window_handles or [])
        except Exception:
            return False
        if not handles:
            return False

        original_handle = None
        try:
            original_handle = driver.current_window_handle
        except Exception:
            pass
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                current_url = str(getattr(driver, "current_url", "") or "").strip().lower()
            except Exception:
                continue
            if current_url in {"", "about:blank", "data:,"} or current_url.startswith("chrome://newtab"):
                tab.window_handle = handle
                window_id = self._top_level_window_id_for_handle(driver, handle)
                self._log_diagnostic(
                    "warm_tab_initial_handle_reused",
                    function="_reuse_initial_handle_for_warm_tab",
                    extra={
                        "external_id": tab.external_id,
                        "url": tab.url,
                        "window_handle": handle,
                        "top_level_window_id": window_id,
                    },
                )
                self._log_diagnostic(
                    "warm_tab_opened",
                    function="_reuse_initial_handle_for_warm_tab",
                    extra={
                        "external_id": tab.external_id,
                        "url": tab.url,
                        "window_handle": handle,
                        "top_level_window_id": window_id,
                        "reused_initial_blank_handle": True,
                    },
                )
                return True
        if original_handle:
            try:
                driver.switch_to.window(original_handle)
            except Exception:
                pass
        return False

    def _open_blank_tab(self) -> str:
        driver = self.ensure_driver()
        try:
            original_handle = driver.current_window_handle
        except Exception:
            original_handle = ""
        expected_window_id = self._top_level_window_id_for_handle(driver, original_handle) if original_handle else None
        before_handles = set(self._driver_handles())
        self._log_diagnostic(
            "warm_tab_new_tab_requested",
            function="_open_blank_tab",
            extra={"original_handle": original_handle, "expected_top_level_window_id": expected_window_id},
        )
        self._log_diagnostic("warm_tab_open_requested", function="_open_blank_tab")
        try:
            if original_handle:
                driver.switch_to.window(original_handle)
            driver.switch_to.new_window("tab")
            handle = driver.current_window_handle
            actual_window_id = self._top_level_window_id_for_handle(driver, handle)
            if expected_window_id is not None and actual_window_id is not None and actual_window_id != expected_window_id:
                self._log_diagnostic(
                    "selenium_second_top_level_window_detected",
                    function="_open_blank_tab",
                    level="ERROR",
                    extra={
                        "window_handle": handle,
                        "expected_top_level_window_id": expected_window_id,
                        "actual_top_level_window_id": actual_window_id,
                    },
                )
                self._close_rejected_window_handle(driver, handle, original_handle)
                cdp_handle = self._open_blank_tab_via_cdp(driver, before_handles, original_handle, expected_window_id)
                if cdp_handle:
                    return cdp_handle
                raise RuntimeError(
                    "selenium_new_tab_created_second_top_level_window: "
                    f"expected={expected_window_id} actual={actual_window_id}"
                )
            self._snapshot_window_handles_locked(reason="warm_tab_opened", log=True)
            self._log_diagnostic(
                "warm_tab_opened",
                function="_open_blank_tab",
                extra={
                    "window_handle": handle,
                    "top_level_window_id": actual_window_id,
                    "method": "selenium_new_window_tab",
                },
            )
            return handle
        except Exception:
            raise

    def _close_rejected_window_handle(self, driver, handle: str, restore_handle: str) -> None:
        try:
            driver.switch_to.window(handle)
            driver.close()
        except Exception:
            self._log_diagnostic(
                "selenium_second_top_level_window_close_failed",
                function="_close_rejected_window_handle",
                level="ERROR",
                extra={"window_handle": handle},
            )
        if restore_handle:
            try:
                driver.switch_to.window(restore_handle)
            except Exception:
                pass

    def _open_blank_tab_via_cdp(
        self,
        driver,
        before_handles: set[str],
        restore_handle: str,
        expected_window_id: int | None,
    ) -> str | None:
        if not hasattr(driver, "execute_cdp_cmd"):
            return None
        self._log_diagnostic(
            "warm_tab_new_tab_requested",
            function="_open_blank_tab_via_cdp",
            extra={"method": "cdp_target_create", "expected_top_level_window_id": expected_window_id},
        )
        try:
            result = driver.execute_cdp_cmd(
                "Target.createTarget",
                {"url": "about:blank", "newWindow": False, "background": False},
            )
        except Exception as exc:
            self._log_diagnostic(
                "warm_tab_open_failed",
                function="_open_blank_tab_via_cdp",
                level="ERROR",
                extra={"error_type": type(exc).__name__, "error": str(exc)},
            )
            return None
        target_id = str(result.get("targetId") or "") if isinstance(result, dict) else ""
        deadline = time.time() + 2.0
        handle = ""
        while time.time() < deadline:
            handles = set(self._driver_handles())
            new_handles = sorted(handles - before_handles)
            if target_id and target_id in handles:
                handle = target_id
                break
            if new_handles:
                handle = new_handles[0]
                break
            time.sleep(0.05)
        if not handle:
            return None
        try:
            driver.switch_to.window(handle)
        except Exception:
            return None
        actual_window_id = self._top_level_window_id_for_handle(driver, handle)
        if expected_window_id is not None and actual_window_id is not None and actual_window_id != expected_window_id:
            self._log_diagnostic(
                "selenium_second_top_level_window_detected",
                function="_open_blank_tab_via_cdp",
                level="ERROR",
                extra={
                    "window_handle": handle,
                    "expected_top_level_window_id": expected_window_id,
                    "actual_top_level_window_id": actual_window_id,
                },
            )
            self._close_rejected_window_handle(driver, handle, restore_handle)
            return None
        self._snapshot_window_handles_locked(reason="warm_tab_opened:cdp", log=True)
        self._log_diagnostic(
            "warm_tab_opened",
            function="_open_blank_tab_via_cdp",
            extra={
                "window_handle": handle,
                "top_level_window_id": actual_window_id,
                "method": "cdp_target_create",
            },
        )
        return handle

    def _load_warm_tab(self, tab: WarmTab, *, reason: str) -> WarmTab:
        if (self.state.busy or self.state.active_worker_action or self.state.active_action) and reason in {"open", "refresh", "preload"}:
            self._log_warm_refresh_skipped_action_running(reason=reason)
            return tab
        now = time.time()
        refresh_started = time.monotonic()
        if now < tab.retry_after_epoch:
            return tab
        if not self._source_load_allowed(tab):
            return tab
        if self._tab_is_quarantined(tab):
            tab.warm_state = "probing"
            tab.access_state = "probing"
        driver = self.ensure_driver()

        try:
            handles = self._driver_handles()
            if not tab.window_handle or tab.window_handle not in handles:
                self._log_diagnostic(
                    "warm_tab_open_requested",
                    function="_load_warm_tab",
                    extra={
                        "site": tab.site,
                        "external_id": tab.external_id,
                        "url": tab.url,
                        "reason": reason,
                    },
                )
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "watchlist_warm_tab_opening",
                    site=tab.site,
                    details={"external_id": tab.external_id, "url": tab.url, "reason": reason},
                )
                reused_initial = self._reuse_initial_handle_for_warm_tab(driver, tab)
                if not reused_initial:
                    tab.window_handle = self._open_blank_tab()
                tab.last_loaded_at = utc_now_iso()
                tab.last_loaded_epoch = now
            else:
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "watchlist_warm_tab_refresh_started",
                    site=tab.site,
                    details={"external_id": tab.external_id, "url": tab.url, "reason": reason},
                )
                driver.switch_to.window(tab.window_handle)

            old_timeout = float(getattr(self.cfg, "worker_wait_timeout_seconds", 20.0) or 20.0)
            try:
                driver.set_page_load_timeout(float(getattr(self.cfg, "watchlist_warm_tab_reload_timeout_seconds", 8.0) or 8.0))
            except Exception:
                pass
            driver.get(tab.url)
            try:
                driver.set_page_load_timeout(old_timeout)
            except Exception:
                pass
            challenge = self._detect_driver_challenge(
                driver,
                source=self._challenge_tab_source(tab),
            )
            if challenge.detected:
                tab.last_refreshed_at = utc_now_iso()
                tab.last_refreshed_epoch = time.time()
                tab.last_refresh_duration_ms = round((time.monotonic() - refresh_started) * 1000, 3)
                self._quarantine_challenged_tab(tab, challenge, context=f"warm_tab_{reason}")
                return tab

            self._record_normal_tab_load(tab, context=f"warm_tab_{reason}")
            if tab.site == "mediamarkt":
                MediaMarktWorkerCase._wait_dom_settle(driver, timeout=2.0)
                tab.last_dom_status = MediaMarktWorkerCase.detect_pdp_dom_status(driver)
            else:
                tab.last_dom_status = "loaded"
            tab.last_refreshed_at = utc_now_iso()
            tab.last_refreshed_epoch = time.time()
            tab.last_refresh_duration_ms = round((time.monotonic() - refresh_started) * 1000, 3)
            tab.warm_state = "ready"
            tab.last_error = ""
            self._snapshot_window_handles_locked(reason=f"warm_tab_loaded:{reason}", log=True)
            self._refresh_process_tracking(reason=f"warm_tab_loaded:{reason}", log_detected=True)
            self._log_diagnostic(
                "warm_tab_opened",
                function="_load_warm_tab",
                extra={
                    "site": tab.site,
                    "external_id": tab.external_id,
                    "url": tab.url,
                    "reason": reason,
                    "window_handle": tab.window_handle,
                    "dom_status": tab.last_dom_status,
                },
            )
            self._log_runtime(
                "INFO",
                "selenium",
                "watchlist_warm_tab_ready" if reason == "open" else "watchlist_warm_tab_refresh_finished",
                site=tab.site,
                details={
                    "external_id": tab.external_id,
                    "url": tab.url,
                    "dom_status": tab.last_dom_status,
                    "window_handle": tab.window_handle,
                    "refresh_duration_ms": tab.last_refresh_duration_ms,
                },
            )
        except Exception as exc:
            tab.warm_state = "failed"
            tab.last_error = f"{type(exc).__name__}: {exc}"
            tab.retry_after_epoch = time.time() + max(15.0, float(getattr(self.cfg, "watchlist_warm_tab_refresh_interval_seconds", 30.0) or 30.0))
            self._log_runtime(
                "WARNING",
                "selenium",
                "watchlist_warm_tab_failed",
                site=tab.site,
                details={"external_id": tab.external_id, "url": tab.url, "error": tab.last_error},
            )
        return tab

    def preload_warm_tabs_now(self) -> dict[str, int | bool | str]:
        if not self._warm_tabs_allowed():
            return {"ok": False, "opened": 0, "reason": "warm_tabs_disabled"}
        if self.state.busy:
            self._log_warm_refresh_skipped_action_running(reason="preload")
            return {"ok": False, "opened": 0, "reason": "action_active"}
        opened = 0
        with self._action_lock:
            with self._warm_tabs_lock:
                tabs = list(self._warm_tabs.values())
            for tab in tabs:
                self._load_warm_tab(tab, reason="open")
                if tab.window_handle and tab.warm_state == "ready":
                    opened += 1
        self._refresh_warm_counts()
        return {"ok": True, "opened": opened}

    def _refresh_warm_counts(self) -> None:
        with self._warm_tabs_lock:
            self.state.warm_tabs_count = sum(1 for tab in self._warm_tabs.values() if tab.window_handle)
            self.state.warm_tabs_max = int(getattr(self.cfg, "watchlist_warm_tabs_max", 6) or 6)
            self.state.warm_tab_urls = [tab.url for tab in self._warm_tabs.values() if tab.window_handle]

    def _log_warm_refresh_skipped_action_running(self, *, reason: str) -> None:
        self.state.warm_refresh_paused_reason = reason or "worker_busy"
        now = time.time()
        if now - self._last_warm_refresh_skipped_action_log_epoch < 5.0:
            return
        self._last_warm_refresh_skipped_action_log_epoch = now
        details = {
            "reason": reason,
            "active_worker_action": self.state.active_worker_action or self.state.active_action,
        }
        self._log_runtime("INFO", "selenium", "warm_refresh_skipped_worker_busy", details=details)
        self._log_runtime("INFO", "selenium", "warm_refresh_skipped_action_running", details=details)
        if self.state.active_worker_action or self.state.active_action:
            self._log_runtime("ERROR", "selenium", "warm_refresh_attempted_while_action_active", details=details)

    def _run_warm_refresh(self, tab: WarmTab, *, reason: str) -> None:
        if self.state.busy or self.state.active_worker_action or self.state.active_action:
            self._log_warm_refresh_skipped_action_running(reason=reason)
            return
        self.state.warm_refresh_running = True
        self.state.warm_refresh_paused_reason = ""
        try:
            self._load_warm_tab(tab, reason=reason)
        finally:
            self.state.warm_refresh_running = False

    def _set_action_active(self, value: str) -> None:
        self.state.active_action = value
        self.state.active_worker_action = value
        if value:
            self.state.warm_refresh_paused_reason = "worker_busy"
        else:
            self.state.warm_refresh_paused_reason = ""

    def _ensure_warm_tab_for_action(self, job: SeleniumJob) -> WarmTab | None:
        key = self._warm_key(job.site, job.target.external_id)
        with self._warm_tabs_lock:
            tab = self._warm_tabs.get(key)
            if tab is None:
                tab = WarmTab(
                    site=job.site,
                    external_id=job.target.external_id,
                    product_title=job.target.title or job.target.external_id,
                    url=job.target.product_url,
                    warm_state="loading",
                )
                self._warm_tabs[key] = tab
                self._warm_targets[key] = tab
        if not tab.window_handle or tab.window_handle not in self._driver_handles():
            self._load_warm_tab(tab, reason="action_missing_warm_tab")
        return tab

    def _driver_matches_target_url_or_id(self, driver, job: SeleniumJob) -> bool:
        try:
            if MediaMarktWorkerCase.article_matches_target(driver, job.target):
                return True
        except Exception:
            pass
        try:
            current_url = str(getattr(driver, "current_url", "") or "")
        except Exception:
            current_url = ""
        expected = str(job.target.product_url or "")
        return bool(expected and current_url and current_url.rstrip("/") == expected.rstrip("/"))

    def _active_driver_tab_is_safe_buyable(self, driver, job: SeleniumJob, trace: WorkerTraceLogger) -> tuple[bool, str]:
        if job.site != "mediamarkt":
            return True, "not_mediamarkt"
        dom_status = MediaMarktWorkerCase.detect_pdp_dom_status(driver)
        if dom_status in {"out_of_stock", "soon_available", "notify_only"}:
            trace.set_result("mediamarkt_warm_action_stale_stock", {"dom_status": dom_status})
            self._log_runtime(
                "WARNING",
                "action",
                "mediamarkt_warm_action_stale_stock",
                site=job.site,
                details={"external_id": job.target.external_id, "dom_status": dom_status},
            )
            return False, dom_status
        if dom_status != "buyable":
            return False, dom_status
        return True, dom_status

    def _warm_tabs_idle_tick(self) -> None:
        if not self._warm_tabs_allowed() or self.stop_event.is_set():
            return
        if self.state.busy or self.state.active_worker_action or self.state.active_action:
            self._log_warm_refresh_skipped_action_running(reason="state_busy")
            return
        now = time.time()
        min_interval = float(getattr(self.cfg, "watchlist_warm_tab_min_refresh_interval_seconds", 15.0) or 15.0)
        if now - self._last_warm_refresh_epoch < min_interval:
            return
        with self._warm_tabs_lock:
            tabs = list(self._warm_tabs.values())
        if not tabs:
            return

        refresh_interval = float(getattr(self.cfg, "watchlist_warm_tab_refresh_interval_seconds", 30.0) or 30.0)
        stale_after = float(getattr(self.cfg, "watchlist_warm_tab_stale_after_seconds", 60.0) or 60.0)
        due: list[WarmTab] = []
        for tab in tabs:
            if now < tab.retry_after_epoch:
                continue
            age = now - max(tab.last_refreshed_epoch, tab.last_loaded_epoch, 0.0)
            if (
                not tab.window_handle
                or tab.warm_state in {"loading", "failed", "challenged", "quarantined", "cooling_down", "probing"}
                or age >= refresh_interval
                or age >= stale_after
            ):
                due.append(tab)
        if not due:
            return
        due.sort(key=lambda tab: max(tab.last_refreshed_epoch, tab.last_loaded_epoch, 0.0))
        if not self._action_lock.acquire(blocking=False):
            self._log_warm_refresh_skipped_action_running(reason="action_lock_busy")
            return
        try:
            self._run_warm_refresh(due[0], reason="refresh" if due[0].window_handle else "open")
            self._last_warm_refresh_epoch = time.time()
            self._refresh_warm_counts()
        finally:
            self._action_lock.release()

    def warm_tabs_snapshot(self) -> list[dict]:
        now = time.time()
        with self._warm_tabs_lock:
            tabs = list(self._warm_tabs.values())
        return [
            {
                "site": tab.site,
                "external_id": tab.external_id,
                "product_title": tab.product_title,
                "url": tab.url,
                "window_handle": tab.window_handle,
                "warm_state": tab.warm_state,
                "last_loaded_at": tab.last_loaded_at,
                "last_refreshed_at": tab.last_refreshed_at,
                "age_seconds": round(max(0.0, now - max(tab.last_refreshed_epoch, tab.last_loaded_epoch, now)), 3),
                "last_refresh_duration_ms": tab.last_refresh_duration_ms,
                "last_dom_status": tab.last_dom_status,
                "last_error": tab.last_error,
                "access_state": tab.access_state,
                "challenge_type": tab.challenge_type,
                "challenge_confidence": tab.challenge_confidence,
                "challenge_reason_code": tab.challenge_reason_code,
                "challenge_signals": list(tab.challenge_signals),
                "challenge_detected_at": tab.challenge_detected_at,
                "manual_action_required": tab.manual_action_required,
                "retry_after_epoch": tab.retry_after_epoch,
            }
            for tab in tabs
        ]

    def refresh_lifecycle_snapshot(self) -> None:
        if self.driver is not None and not self.state.busy and not self.state.warm_refresh_running:
            if self._action_lock.acquire(blocking=False):
                try:
                    with self._driver_lock:
                        self._snapshot_window_handles_locked(reason="lifecycle_snapshot")
                finally:
                    self._action_lock.release()
        self._refresh_process_tracking(reason="lifecycle_snapshot")

    def _prepare_mediamarkt_warm_action(
        self,
        job: SeleniumJob,
        trace: WorkerTraceLogger,
        started_at: float,
        timeline: WorkerActionTimeline | None = None,
    ) -> bool:
        key = self._warm_key(job.site, job.target.external_id)
        action_id = timeline.action_id if timeline is not None else getattr(trace, "action_id", job.action_id)
        self._last_mediamarkt_warm_switch_monotonic = 0.0
        self._last_mediamarkt_hot_path_started_monotonic = time.monotonic()
        self._last_mediamarkt_warm_tab_state = {}
        if timeline is not None:
            timeline.record(
                "mediamarkt_hot_path_started",
                step_name="mediamarkt_hot_path",
                source=dict(job.metadata).get("source", ""),
            )
        if timeline is not None:
            timeline.record(
                "mediamarkt_warm_tab_lookup_started",
                step_name="warm_tab_lookup_ms",
                external_id=job.target.external_id,
            )
        lookup_started = time.monotonic()
        with self._warm_tabs_lock:
            tab = self._warm_tabs.get(key)
        lookup_ms = (time.monotonic() - lookup_started) * 1000
        if tab is None or not tab.window_handle:
            if timeline is not None:
                timeline.record(
                    "mediamarkt_warm_tab_lookup_finished",
                    step_name="warm_tab_lookup_ms",
                    result="missing",
                    duration_ms=lookup_ms,
                    warm_tab_found=False,
                )
            self._log_runtime(
                "INFO",
                "action",
                "watchlist_action_warm_tab_missing",
                site=job.site,
                details={"action_id": action_id, "external_id": job.target.external_id, "warm_tab_found": False},
            )
            return False

        source_snapshot = self._challenge_access.snapshot(self._challenge_site_source(job.site))
        if self._tab_is_quarantined(tab) or source_snapshot.get("state") in {
            "challenged",
            "cooling_down",
            "probing",
            "manually_blocked",
        }:
            self._block_action_for_challenge(
                job=job,
                tab=tab,
                trace=trace,
                reason_code=tab.challenge_reason_code or str(source_snapshot.get("last_reason_code") or "source_cooldown"),
            )

        driver = self.ensure_driver()
        try:
            if tab.window_handle not in self._driver_handles():
                tab.window_handle = ""
                tab.warm_state = "stale"
                if timeline is not None:
                    timeline.record(
                        "mediamarkt_warm_tab_lookup_finished",
                        step_name="warm_tab_lookup_ms",
                        result="stale_handle",
                        duration_ms=lookup_ms,
                        warm_tab_found=False,
                    )
                return False
            now = time.time()
            warm_reference_epoch = max(tab.last_refreshed_epoch, tab.last_loaded_epoch, 0.0)
            warm_age_ms = round((max(0.0, now - warm_reference_epoch) if warm_reference_epoch else 1_000_000.0) * 1000, 3)
            warm_tab_details = {
                "action_id": action_id,
                "external_id": job.target.external_id,
                "warm_tab_found": True,
                "warm_tab_handle": tab.window_handle,
                "warm_tab_url": tab.url,
                "warm_tab_last_refresh_at": tab.last_refreshed_at,
                "warm_tab_age_ms": warm_age_ms,
                "warm_tab_last_refresh_duration_ms": tab.last_refresh_duration_ms,
                "lookup_duration_ms": round(lookup_ms, 3),
            }
            self._last_mediamarkt_warm_tab_state = dict(warm_tab_details)
            if timeline is not None:
                timeline.record(
                    "mediamarkt_warm_tab_lookup_finished",
                    step_name="warm_tab_lookup_ms",
                    result="success",
                    duration_ms=lookup_ms,
                    **warm_tab_details,
                )

            switch_started = time.monotonic()
            if timeline is not None:
                timeline.record(
                    "mediamarkt_warm_tab_switch_started",
                    step_name="switch_to_warm_tab_ms",
                    warm_tab_handle=tab.window_handle,
                    warm_tab_url=tab.url,
                )
            driver.switch_to.window(tab.window_handle)
            self._last_mediamarkt_warm_switch_monotonic = time.monotonic()
            challenge = self._detect_driver_challenge(
                driver,
                source=self._challenge_tab_source(tab),
            )
            if challenge.detected:
                self._quarantine_challenged_tab(tab, challenge, context="warm_action_after_switch")
                self._block_action_for_challenge(
                    job=job,
                    tab=tab,
                    trace=trace,
                    reason_code=tab.challenge_reason_code,
                )
            switch_ms = (self._last_mediamarkt_warm_switch_monotonic - switch_started) * 1000
            if timeline is not None:
                timeline.record(
                    "mediamarkt_warm_tab_switch_finished",
                    step_name="switch_to_warm_tab_ms",
                    result="success",
                    duration_ms=switch_ms,
                    warm_tab_handle=tab.window_handle,
                    warm_tab_url=tab.url,
                    switch_duration_ms=round(switch_ms, 3),
                    refresh_policy=getattr(self.cfg, "mediamarkt_fast_action_refresh_policy", "never_if_warm_recent"),
                    **driver_context(driver),
                )
            trace.step("watchlist_action_using_warm_tab", {"phase": "warm_tab", "url": ""}, level="verbose")
            policy = str(getattr(self.cfg, "mediamarkt_fast_action_refresh_policy", "never_if_warm_recent") or "never_if_warm_recent").strip().lower()
            if policy not in {"never_if_warm_recent", "micro_revalidate_only", "always_refresh"}:
                policy = "never_if_warm_recent"
            recent_threshold = max(0.0, float(getattr(self.cfg, "mediamarkt_warm_recent_threshold_seconds", 60.0) or 60.0))
            now_for_age = time.time()
            reference_epoch = max(tab.last_refreshed_epoch, tab.last_loaded_epoch, 0.0)
            age_seconds = max(0.0, now_for_age - reference_epoch) if reference_epoch else 1_000_000.0
            refresh_details = {
                "action_id": action_id,
                "external_id": job.target.external_id,
                "refresh_policy": policy,
                "warm_tab_age_ms": round(age_seconds * 1000, 3),
                "warm_recent_threshold_seconds": recent_threshold,
            }

            refresh_reason = ""
            validation: dict = {}
            button = None
            dom_ms = 0.0
            if self._driver_matches_target_url_or_id(driver, job):
                dom_started = time.monotonic()
                button, validation = MediaMarktWorkerCase.fast_revalidate_buy_button(driver, target=job.target, timeline=timeline)
                dom_ms = (time.monotonic() - dom_started) * 1000
                previous_dom_status = tab.last_dom_status
                tab.last_dom_status = "buyable" if button is not None else str(validation.get("reason") or "unknown")
                if button is not None:
                    refresh_details.update(
                        {
                            "full_refresh_skipped": True,
                            "skip_reason": "warm_tab_visible_enabled_button_already_available",
                            "availability_decision": validation.get("availability_decision") or {},
                        }
                    )
                    if timeline is not None:
                        timeline.record(
                            "mediamarkt_optional_refresh_skipped",
                            step_name="optional_refresh_ms",
                            result="skipped",
                            duration_ms=0,
                            **refresh_details,
                            **driver_context(driver),
                        )
                        if dom_ms > 500:
                            timeline.record(
                                "mediamarkt_hot_path_slow_step",
                                step_name="dom_revalidate_ms",
                                duration_ms=dom_ms,
                                threshold_ms=500,
                                **driver_context(driver),
                            )
                    return True
                decision = validation.get("availability_decision") or {}
                if decision.get("decision") == "unavailable" and validation.get("rejection_markers"):
                    stale_status = (
                        previous_dom_status
                        if previous_dom_status in {"out_of_stock", "soon_available", "notify_only"}
                        else validation.get("reason")
                    )
                    trace.set_result(
                        "mediamarkt_warm_action_stale_stock",
                        {
                            "dom_status": stale_status,
                            "markers": validation.get("rejection_markers"),
                            "availability_decision": decision,
                        },
                    )
                    raise RuntimeError(f"mediamarkt_warm_action_stale_stock: {validation.get('rejection_markers')}")
                refresh_reason = "visible_enabled_button_missing"
            else:
                refresh_reason = "warm_tab_url_or_article_mismatch"
                if timeline is not None:
                    timeline.record(
                        "mediamarkt_fast_revalidate_finished",
                        step_name="dom_revalidate_ms",
                        result="target_mismatch",
                        reason=refresh_reason,
                        **driver_context(driver),
                    )

            if policy == "micro_revalidate_only" and refresh_reason != "warm_tab_url_or_article_mismatch":
                if timeline is not None:
                    timeline.record(
                        "mediamarkt_optional_refresh_skipped",
                        step_name="optional_refresh_ms",
                        result="skipped",
                        duration_ms=0,
                        full_refresh_skipped=True,
                        skip_reason="micro_revalidate_only",
                        validation_reason=validation.get("reason"),
                        **refresh_details,
                        **driver_context(driver),
                    )
                return False

            refresh_details.update(
                {
                    "full_refresh_skipped": False,
                    "refresh_reason": refresh_reason,
                    "validation_reason": validation.get("reason"),
                    "availability_decision": validation.get("availability_decision") or {},
                }
            )
            if timeline is not None and dom_ms > 500:
                timeline.record(
                    "mediamarkt_hot_path_slow_step",
                    step_name="dom_revalidate_ms",
                    duration_ms=dom_ms,
                    threshold_ms=500,
                    **driver_context(driver),
                )
            should_refresh = bool(refresh_reason)
            if should_refresh:
                refresh_started = time.monotonic()
                from_url = self._safe_driver_url()
                refresh_method = "get" if refresh_reason == "warm_tab_url_or_article_mismatch" else "refresh"
                if timeline is not None:
                    timeline.record(
                        "mediamarkt_optional_refresh_started",
                        step_name="optional_refresh_ms",
                        navigation_name="warm_tab_optional_refresh",
                        from_url=from_url,
                        method=refresh_method,
                        **driver_context(driver),
                        **refresh_details,
                    )
                if refresh_method == "get":
                    driver.get(tab.url)
                else:
                    driver.refresh()
                try:
                    MediaMarktWorkerCase._wait_for_fast_refresh_markers(
                        driver,
                        timeline=timeline,
                        timeout=min(2.0, float(getattr(self.cfg, "watchlist_warm_tab_reload_timeout_seconds", 8.0) or 8.0)),
                    )
                except Exception:
                    pass
                challenge = self._detect_driver_challenge(
                    driver,
                    source=self._challenge_tab_source(tab),
                )
                if challenge.detected:
                    self._quarantine_challenged_tab(tab, challenge, context="warm_action_after_refresh")
                    self._block_action_for_challenge(
                        job=job,
                        tab=tab,
                        trace=trace,
                        reason_code=tab.challenge_reason_code,
                    )
                refresh_ms = (time.monotonic() - refresh_started) * 1000
                tab.last_refreshed_at = utc_now_iso()
                tab.last_refreshed_epoch = time.time()
                tab.last_refresh_duration_ms = round(refresh_ms, 3)
                refresh_details.update(
                    {
                        "full_refresh_skipped": False,
                        "optional_refresh_ms": round(refresh_ms, 3),
                        "from_url": from_url,
                        "to_url": self._safe_driver_url(),
                        "method": refresh_method,
                    }
                )
                if timeline is not None:
                    timeline.record(
                        "mediamarkt_optional_refresh_finished",
                        step_name="optional_refresh_ms",
                        result="success",
                        duration_ms=refresh_ms,
                        navigation_name="warm_tab_optional_refresh",
                        important_markers_detected={
                            "unavailable": detect_mediamarkt_unavailable_markers(driver),
                            "queue": detect_queue_markers(driver, "mediamarkt"),
                        },
                        **driver_context(driver),
                        **refresh_details,
                    )
            else:
                if timeline is not None:
                    timeline.record(
                        "mediamarkt_optional_refresh_skipped",
                        step_name="optional_refresh_ms",
                        result="skipped",
                        duration_ms=0,
                        **refresh_details,
                        **driver_context(driver),
                    )
                self._log_runtime(
                    "INFO",
                    "action",
                    "mediamarkt_optional_refresh_skipped",
                    site=job.site,
                    details=refresh_details,
                )

            dom_started = time.monotonic()
            button, validation = MediaMarktWorkerCase.fast_revalidate_buy_button(driver, target=job.target, timeline=timeline)
            dom_ms = (time.monotonic() - dom_started) * 1000
            previous_dom_status = tab.last_dom_status
            tab.last_dom_status = "buyable" if button is not None else str(validation.get("reason") or "unknown")
            if button is None:
                decision = validation.get("availability_decision") or {}
                if decision.get("decision") == "unavailable" and validation.get("rejection_markers"):
                    stale_status = (
                        previous_dom_status
                        if previous_dom_status in {"out_of_stock", "soon_available", "notify_only"}
                        else validation.get("reason")
                    )
                    trace.set_result(
                        "mediamarkt_warm_action_stale_stock",
                        {
                            "dom_status": stale_status,
                            "markers": validation.get("rejection_markers"),
                            "availability_decision": decision,
                        },
                    )
                    raise RuntimeError(f"mediamarkt_warm_action_stale_stock: {validation.get('rejection_markers')}")
                if timeline is not None:
                    timeline.record(
                        "mediamarkt_hot_path_slow_step" if dom_ms > 500 else "mediamarkt_fast_revalidate_rejected",
                        step_name="dom_revalidate_ms",
                        result="rejected",
                        duration_ms=dom_ms,
                        validation_reason=validation.get("reason"),
                        **driver_context(driver),
                    )
                return False
            if dom_ms > 500 and timeline is not None:
                timeline.record(
                    "mediamarkt_hot_path_slow_step",
                    step_name="dom_revalidate_ms",
                    duration_ms=dom_ms,
                    threshold_ms=500,
                    **driver_context(driver),
                )
            return True
        except RuntimeError:
            raise
        except Exception as exc:
            tab.warm_state = "failed"
            tab.last_error = f"{type(exc).__name__}: {exc}"
            self._log_runtime(
                "WARNING",
                "action",
                "watchlist_warm_tab_failed",
                site=job.site,
                details={"external_id": job.target.external_id, "error": tab.last_error},
            )
            return False

    def init_driver(self):
        with self._driver_lock:
            if self.driver is not None:
                logger.info("selenium_driver_create_skipped_existing_driver")
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "selenium_driver_create_skipped_existing_driver",
                    details={"driver_session_id": self.state.driver_session_id},
                )
                self._log_runtime(
                    "INFO",
                    "selenium",
                    "selenium_existing_driver_reused",
                    details={"driver_session_id": self.state.driver_session_id},
                )
                return self.driver
            self._close_old_app_driver_processes(reason="before_driver_create")

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()

        if self.cfg.chrome_binary:
            options.binary_location = self.cfg.chrome_binary

        options.add_experimental_option(
            "prefs",
            {
                "profile.managed_default_content_settings.images": 2,
            },
        )

        options.add_argument(self._app_process_marker)
        # Keep Chrome/Selenium's normal automation indicators. Challenge recovery
        # is intentionally limited to detection, cooldown, and controlled probes.

        if self.cfg.chrome_user_data_dir:
            options.add_argument(f"--user-data-dir={self.cfg.chrome_user_data_dir}")
        if self.cfg.chrome_profile_dir:
            options.add_argument(f"--profile-directory={self.cfg.chrome_profile_dir}")
        if self.cfg.proxy_enabled and self.cfg.proxy_host and self.cfg.proxy_port > 0:
            options.add_argument(
                f"--proxy-server={self.cfg.proxy_type or 'http'}://{self.cfg.proxy_host}:{self.cfg.proxy_port}"
            )

        chromedriver_log_path = self._chromedriver_log_path()
        diagnostics = collect_selenium_diagnostics(
            self.cfg,
            options=options,
            chromedriver_log_path=chromedriver_log_path,
            base_dir=getattr(self.cfg, "base_dir", None),
        )
        logger.info("[selenium] driver startup diagnostics=%s", json.dumps(diagnostics.to_dict(), ensure_ascii=False))

        def create_driver():
            service = Service(service_args=["--verbose"], log_output=str(chromedriver_log_path))
            self._log_diagnostic(
                "chrome_process_spawn_requested",
                function="init_driver.create_driver",
                extra={
                    "chromedriver_log_path": str(chromedriver_log_path),
                    "chrome_user_data_dir": getattr(self.cfg, "chrome_user_data_dir", ""),
                    "chrome_profile_dir": getattr(self.cfg, "chrome_profile_dir", ""),
                },
            )
            driver = webdriver.Chrome(service=service, options=options)
            self._log_diagnostic(
                "chrome_process_spawned",
                function="init_driver.create_driver",
                extra={
                    "chromedriver_log_path": str(chromedriver_log_path),
                    "chrome_user_data_dir": getattr(self.cfg, "chrome_user_data_dir", ""),
                    "chrome_profile_dir": getattr(self.cfg, "chrome_profile_dir", ""),
                    "driver_session_id": self._safe_driver_session_id(driver),
                },
            )
            return driver

        try:
            self._log_diagnostic("selenium_driver_create_requested", function="init_driver")
            driver = create_driver()
        except Exception as exc:
            snapshot_path = write_selenium_startup_failure_snapshot(
                cfg=self.cfg,
                diagnostics=diagnostics,
                exc=exc,
                base_dir=getattr(self.cfg, "base_dir", None),
            )
            error_text = str(exc)
            profile_lock_detected = any(
                needle in error_text.lower()
                for needle in (
                    "user data directory is already in use",
                    "profile",
                    "lock",
                    "cannot create default profile directory",
                )
            )
            prefix = "driver_startup_failed_profile_lock" if profile_lock_detected else "driver_startup_failed"
            self.state.last_error = f"{prefix}: {type(exc).__name__}: {exc}"
            self.state.last_diagnostic_snapshot = str(snapshot_path)
            self._log_runtime(
                "ERROR",
                "error",
                (
                    "selenium profile lock detected; another Chrome may own the configured profile"
                    if profile_lock_detected
                    else f"selenium driver startup failed error={type(exc).__name__}: {exc}"
                ),
                details={
                    "error_type": type(exc).__name__,
                    "error": error_text,
                    "profile_lock_detected": profile_lock_detected,
                    "chrome_user_data_dir": getattr(self.cfg, "chrome_user_data_dir", ""),
                    "chrome_profile_dir": getattr(self.cfg, "chrome_profile_dir", ""),
                    "diagnostic_snapshot": str(snapshot_path),
                    "likely_causes": list(LIKELY_CHROME_FAILURE_CAUSES),
                },
            )
            if profile_lock_detected:
                self._log_runtime(
                    "WARNING",
                    "selenium",
                    "selenium_profile_lock_detected",
                    details={
                        "error_type": type(exc).__name__,
                        "error": error_text,
                        "chrome_user_data_dir": getattr(self.cfg, "chrome_user_data_dir", ""),
                        "chrome_profile_dir": getattr(self.cfg, "chrome_profile_dir", ""),
                    },
                )
                if self._close_old_app_driver_processes(reason="profile_lock_detected"):
                    try:
                        driver = create_driver()
                    except Exception:
                        logger.exception("[selenium] webdriver.Chrome retry after closing old app driver failed")
                        raise
                else:
                    logger.exception(
                        "[selenium] webdriver.Chrome failed diagnostic_snapshot=%s likely_causes=%s",
                        snapshot_path,
                        list(LIKELY_CHROME_FAILURE_CAUSES),
                    )
                    raise
            else:
                logger.exception(
                    "[selenium] webdriver.Chrome failed diagnostic_snapshot=%s likely_causes=%s",
                    snapshot_path,
                    list(LIKELY_CHROME_FAILURE_CAUSES),
                )
                raise

        self.state.last_error = ""
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                """
            },
        )

        driver.set_page_load_timeout(30)
        driver.set_script_timeout(20)
        self._record_driver_started(driver)
        self._log_diagnostic("selenium_driver_created", function="init_driver")
        return driver

    def rebuild_driver(self) -> None:
        with self._driver_lock:
            if self.driver is not None:
                self.close_driver(reason="rebuild")

            self._log_runtime("INFO", "selenium", "selenium_driver_create_started", details={"reason": "rebuild"})
            self.driver = self.init_driver()
            self.state.driver_rebuilds += 1
            self._log_runtime(
                "INFO",
                "selenium",
                "selenium_driver_create_finished",
                details={
                    "reason": "rebuild",
                    "driver_session_id": self.state.driver_session_id,
                    "browser_started_at": self.state.browser_started_at,
                    "last_driver_create_at": self.state.last_driver_create_at,
                    "chromedriver_pid": self.state.chromedriver_pid,
                    "chrome_pid": self.state.chrome_pid,
                    "selenium_window_count": self.state.selenium_window_count,
                },
            )
            self._log_runtime(
                "INFO",
                "selenium",
                f"driver rebuilt rebuilds={self.state.driver_rebuilds}",
            )

    def resolve_handler(self, job: SeleniumJob):
        if job.site not in WORKER_HANDLERS:
            raise RuntimeError(f"no handler for site={job.site}")

        handler_cls = WORKER_HANDLERS[job.site]
        if not hasattr(handler_cls, job.case):
            raise RuntimeError(f"no case registered for site={job.site} case={job.case}")

        handler = getattr(handler_cls, job.case)
        if not callable(handler):
            raise RuntimeError(f"handler is not callable for site={job.site} case={job.case}")

        return handler

    def submit(self, job: SeleniumJob) -> None:
        self.job_queue.put(job)

    def _derive_deny_kind(self, exc: Exception) -> str:
        name = type(exc).__name__
        message = str(exc).lower()

        if "timeout" in name.lower() or "timeout" in message:
            return "timeout"
        if "out_of_stock_after_cart" in message or "niet verkrijgbaar" in message:
            return "out_of_stock_after_cart"
        if "queue_detected" in message:
            return "queue_detected"
        if "bot" in message or "blocked" in message:
            return "bot_blocked"
        if "captcha" in message:
            return "captcha"
        if "challenge" in message:
            return "challenge"
        if "403" in message:
            return "http_403_like"
        if "429" in message:
            return "http_429_like"
        if "stale" in message:
            return "stale_dom"
        if "iframe" in message:
            return "iframe_error"
        if "navigation" in message:
            return "navigation_error"
        return name

    def run(self) -> None:
        try:
            self.state.started = True
            self.state.last_start_at = self.state.last_start_at or utc_now_iso()
            self.state.lifecycle_state = "starting"
            self._log_runtime(
                "INFO",
                "selenium",
                f"worker thread started queue_size={self.job_queue.qsize()}",
            )
            self._log_runtime("INFO", "selenium", f"worker started queue_size={self.job_queue.qsize()}")
            self.state.ready = True
            self.state.lifecycle_state = "running"
            self.ready_event.set()

            while not self.stop_event.is_set():
                try:
                    job = self.job_queue.get(timeout=0.5)
                except queue.Empty:
                    self._warm_tabs_idle_tick()
                    continue

                logger.info(
                    "[selenium] job received queue_size=%s site=%s case=%s external_id=%s action_id=%s target_url=%s",
                    self.job_queue.qsize(),
                    job.site,
                    job.case,
                    job.target.external_id,
                    job.action_id,
                    job.target.product_url,
                )
                job_received_monotonic = time.monotonic()
                if self.dispatcher is not None:
                    self.dispatcher.mark_started(job)

                self.state.busy = True
                self._set_action_active(f"{job.site}:{job.case}:{job.target.external_id}")
                self.state.last_job = f"{job.site}:{job.case}"
                self.state.last_result = None

                timeout_seconds = 300.0
                if self.antiban is not None:
                    site_timeout = self.antiban.get_job_timeout_seconds(job.site)
                    if site_timeout is not None:
                        timeout_seconds = site_timeout

                timed_out = {"value": False}
                handler_error: Exception | None = None
                trace = WorkerTraceLogger(
                    cfg=self.cfg,
                    job=job,
                    storage=self.storage,
                    notifier=self.notifier,
                    action_id=job.action_id or None,
                )
                timeline = WorkerActionTimeline(cfg=self.cfg, job=job, action_id=trace.action_id)
                timeline.record(
                    "job_received",
                    step_name="job_received",
                    queue_size=self.job_queue.qsize(),
                    target_url=job.target.product_url,
                    metadata=dict(job.metadata),
                )

                def watchdog():
                    timed_out["value"] = True
                    self.close_driver(reason="job_timeout_watchdog")

                timer = threading.Timer(timeout_seconds, watchdog)
                timer.daemon = True
                timer.start()

                started_at = time.monotonic()
                queued_for_seconds = self._queued_for_seconds(job.created_at)
                action_started_logged = False

                def record_action_started() -> None:
                    nonlocal action_started_logged
                    if action_started_logged:
                        return
                    action_started_logged = True
                    trace.start(
                        {
                            "timeout_seconds": timeout_seconds,
                            "case": job.case,
                            "external_id": job.target.external_id,
                            "action_id": trace.action_id,
                            "created_at": job.created_at,
                            "queued_for_seconds": queued_for_seconds,
                        }
                    )
                    logger.info(
                        "[selenium] starting job site=%s case=%s external_id=%s timeout=%.1fs",
                        job.site,
                        job.case,
                        job.target.external_id,
                        timeout_seconds,
                    )
                    self._log_runtime(
                        "INFO",
                        "action",
                        f"action_job_started site={job.site} external_id={job.target.external_id} action_id={trace.action_id} queued_for={queued_for_seconds}s",
                        site=job.site,
                        details={
                            "event": "action_job_started",
                            "case": job.case,
                            "external_id": job.target.external_id,
                            "title": job.target.title,
                            "action_id": trace.action_id,
                            "created_at": job.created_at,
                            "queued_for_seconds": queued_for_seconds,
                            "timeout_seconds": timeout_seconds,
                            "metadata": dict(job.metadata),
                        },
                    )
                    if self.storage is not None:
                        self.storage.insert_action_log(
                            job.site,
                            job.target.external_id,
                            "selenium_worker",
                            job.case,
                            "started",
                            json.dumps(
                                {
                                    "event": "action_job_started",
                                    "action_id": trace.action_id,
                                    "created_at": job.created_at,
                                    "queued_for_seconds": queued_for_seconds,
                                    "timeout_seconds": timeout_seconds,
                                    "title": job.target.title,
                                    "metadata": dict(job.metadata),
                                },
                                ensure_ascii=False,
                            ),
                        )

                try:
                    action_lock_acquired = False
                    try:
                        action_lock_wait_started = time.monotonic()
                        lock_wait_context = driver_context(self.driver) if self.driver is not None else {}
                        timeline.record(
                            "action_lock_wait_started",
                            step_name="action_lock_wait_ms",
                            **lock_wait_context,
                        )
                        self._action_lock.acquire()
                        action_lock_acquired = True
                        action_lock_wait_ms = (time.monotonic() - action_lock_wait_started) * 1000
                        lock_acquired_context = driver_context(self.driver) if self.driver is not None else {}
                        timeline.record(
                            "action_lock_acquired",
                            step_name="action_lock_wait_ms",
                            result="success",
                            duration_ms=action_lock_wait_ms,
                            action_lock_wait_ms=round(action_lock_wait_ms, 3),
                            **lock_acquired_context,
                        )
                        if job.site == "mediamarkt":
                            timeline.record(
                                "mediamarkt_action_lock_acquired",
                                step_name="action_lock_wait_ms",
                                result="success",
                                duration_ms=action_lock_wait_ms,
                                action_lock_wait_ms=round(action_lock_wait_ms, 3),
                            )
                        defer_action_started_log = job.site == "mediamarkt" and job.case in {
                            "add_to_cart",
                            "add_to_cart_and_checkout",
                        }
                        if not defer_action_started_log:
                            self._log_runtime(
                                "INFO",
                                "action",
                                "action_lock_acquired",
                                site=job.site,
                                details={
                                    "action_id": trace.action_id,
                                    "external_id": job.target.external_id,
                                    "action_lock_wait_ms": round(action_lock_wait_ms, 3),
                                },
                            )
                        if not defer_action_started_log:
                            record_action_started()
                        ensure_started = time.monotonic()
                        if self.driver is None or not self._driver_is_healthy():
                            self.ensure_driver()
                        ensure_ms = (time.monotonic() - ensure_started) * 1000
                        ensure_context = driver_context(self.driver) if self.driver is not None else {}
                        timeline.record(
                            "ensure_driver_finished",
                            step_name="ensure_driver_ms",
                            result="success",
                            duration_ms=ensure_ms,
                            ensure_driver_ms=round(ensure_ms, 3),
                            **ensure_context,
                        )

                        driver_ready_at = utc_now_iso()
                        trace.step(
                            "Browser/session prepared",
                            {
                                "url": "" if defer_action_started_log else self._safe_driver_url(),
                                "driver_ready_at": driver_ready_at,
                                "latency_seconds": round(time.monotonic() - started_at, 3),
                            },
                            level="verbose" if defer_action_started_log else "normal",
                        )
                        should_run_handler = self._prepare_pocketgames_purchase(job, trace)
                        if should_run_handler:
                            warm_path = False
                            if job.site == "mediamarkt" and job.case in {"add_to_cart", "add_to_cart_and_checkout"}:
                                warm_path = self._prepare_mediamarkt_warm_action(job, trace, started_at, timeline=timeline)

                            if warm_path:
                                first_button_search_at = utc_now_iso()
                                self.state.last_button_search_latency_seconds = round(time.monotonic() - started_at, 3)
                                MediaMarktWorkerCase.add_to_cart_from_current_page(
                                    self.driver,
                                    job.target,
                                    self.cfg,
                                    trace,
                                    timeline=timeline,
                                    job_received_monotonic=job_received_monotonic,
                                    warm_tab_switched_monotonic=self._last_mediamarkt_warm_switch_monotonic or None,
                                    hot_path_started_monotonic=self._last_mediamarkt_hot_path_started_monotonic or None,
                                )
                                record_action_started()
                                self._log_runtime(
                                    "INFO",
                                    "action",
                                    "first_button_search",
                                    site=job.site,
                                    details={
                                        "action_id": trace.action_id,
                                        "external_id": job.target.external_id,
                                        "action_path": "warm_tab",
                                        "first_button_search_at": first_button_search_at,
                                        "latency_seconds": self.state.last_button_search_latency_seconds,
                                    },
                                )
                                if job.case == "add_to_cart_and_checkout":
                                    MediaMarktWorkerCase.checkout(
                                        self.driver,
                                        job.target,
                                        self.cfg,
                                        trace,
                                        timeline=timeline,
                                        job_received_monotonic=job_received_monotonic,
                                    )
                            else:
                                record_action_started()
                                if job.site == "mediamarkt":
                                    self._log_runtime(
                                        "INFO",
                                        "action",
                                        "watchlist_action_warm_tab_missing",
                                        site=job.site,
                                        details={"action_id": trace.action_id, "external_id": job.target.external_id},
                                    )
                                handler = self.resolve_handler(job)
                                handler(self.driver, job.target, self.cfg, trace)

                        if should_run_handler and self.antiban is not None:
                            self.antiban.report_success(job.site, "worker")
                    finally:
                        if action_lock_acquired:
                            self._action_lock.release()
                            release_context = driver_context(self.driver) if self.driver is not None else {}
                            timeline.record(
                                "action_lock_released",
                                step_name="action_lock_release",
                                result="success",
                                action_lock_wait_ms=timeline.timings.get("action_lock_wait_ms"),
                                **release_context,
                            )
                            self._log_runtime(
                                "INFO",
                                "action",
                                "action_lock_released",
                                site=job.site,
                                details={
                                    "action_id": trace.action_id,
                                    "external_id": job.target.external_id,
                                    "action_lock_wait_ms": timeline.timings.get("action_lock_wait_ms"),
                                },
                            )

                except Exception as exc:
                    handler_error = exc
                    self.state.last_error = f"{type(exc).__name__}: {exc}"
                    artifact_paths = []
                    if not isinstance(exc, ChallengeBlockedError):
                        artifact_paths = self._dump_failure_artifacts(
                            job,
                            trace,
                            timeline=timeline,
                            total_duration_seconds=time.monotonic() - started_at,
                        )
                    if artifact_paths:
                        logger.error(
                            "[selenium] checkout failure artifacts action_id=%s paths=%s",
                            trace.action_id,
                            artifact_paths,
                        )
                        self._log_runtime(
                            "ERROR",
                            "worker_trace",
                            f"checkout failure artifacts action_id={trace.action_id}",
                            site=job.site,
                            details={
                                "action_id": trace.action_id,
                                "external_id": job.target.external_id,
                                "paths": artifact_paths,
                            },
                        )
                    self._record_pocketgames_worker_failure(job, exc, timed_out=timed_out["value"])
                    purchase_status = self._pocketgames_purchase_status(job)
                    if purchase_status in {PURCHASE_STATUS_UNKNOWN_REVIEW, PURCHASE_STATUS_FAILED}:
                        trace.set_result(
                            purchase_status,
                            {
                                "purchase_key": purchase_key_for_target(job.target),
                                "reason": f"{type(exc).__name__}: {exc}",
                                "url": self._safe_driver_url(),
                            },
                        )
                    if not timed_out["value"]:
                        if trace.result_status == PURCHASE_STATUS_UNKNOWN_REVIEW:
                            trace.warning(
                                "PocketGames result unknown after worker error. Manual review required.",
                                {
                                    "reason": f"{type(exc).__name__}: {exc}",
                                    "url": self._safe_driver_url(),
                                },
                                level="minimal",
                            )
                        elif trace.result_status in CONTROLLED_WORKER_RESULTS:
                            pass
                        else:
                            trace.error(
                                "Worker failed",
                                {
                                    "reason": f"{type(exc).__name__}: {exc}",
                                    "url": self._safe_driver_url(),
                                },
                            )

                    if self.antiban is not None and trace.result_status != PURCHASE_STATUS_UNKNOWN_REVIEW:
                        deny_kind = self._derive_deny_kind(exc)
                        cooldown = self.antiban.report_deny(job.site, "worker", deny_kind)
                        self._log_runtime(
                            "WARNING",
                            "error",
                            f"worker deny case={job.case} external_id={job.target.external_id} deny_kind={deny_kind} cooldown={cooldown:.1f}s",
                            site=job.site,
                            details={
                                "case": job.case,
                                "external_id": job.target.external_id,
                                "deny_kind": deny_kind,
                                "cooldown_seconds": cooldown,
                            },
                        )

                finally:
                    timer.cancel()

                    duration = time.monotonic() - started_at
                    self.state.last_duration_seconds = round(duration, 2)

                    if timed_out["value"]:
                        self.state.jobs_timed_out += 1
                        self._record_pocketgames_worker_failure(job, handler_error, timed_out=True)
                        self.state.last_result = trace.result_status or (
                            PURCHASE_STATUS_UNKNOWN_REVIEW
                            if job.site == "pocketgames"
                            and self.storage is not None
                            and (
                                self.storage.get_purchase_state(job.site, purchase_key_for_target(job.target)) or {}
                            ).get("status") == PURCHASE_STATUS_UNKNOWN_REVIEW
                            else "timed_out"
                        )
                        self.state.last_error = self.state.last_error or "job timed out"
                        trace.error(
                            "Worker timed out",
                            {
                                "reason": "job timed out",
                                "duration_seconds": round(duration, 2),
                            },
                        )
                        self._log_runtime(
                            "WARNING",
                            "error",
                            f"job timed out case={job.case} external_id={job.target.external_id} duration={duration:.2f}s",
                            site=job.site,
                            details={
                                "case": job.case,
                                "external_id": job.target.external_id,
                                "duration_seconds": round(duration, 2),
                            },
                        )
                        if self.storage is not None:
                            self.storage.insert_action_log(
                                job.site,
                                job.target.external_id,
                                "selenium_worker",
                                job.case,
                                "timed_out",
                                json.dumps(
                                    {"duration_seconds": round(duration, 2)},
                                    ensure_ascii=False,
                                ),
                            )
                    elif handler_error is not None:
                        self.state.jobs_failed += 1
                        result_status = trace.result_status or "failed"
                        self.state.last_result = result_status
                        controlled_action_results = {PURCHASE_STATUS_UNKNOWN_REVIEW, *CONTROLLED_WORKER_RESULTS}
                        log_category = "action" if result_status in controlled_action_results else "error"
                        self._log_runtime(
                            "WARNING",
                            log_category,
                            f"job failed case={job.case} external_id={job.target.external_id} status={result_status} duration={duration:.2f}s error={handler_error}",
                            site=job.site,
                            details={
                                "case": job.case,
                                "external_id": job.target.external_id,
                                "status": result_status,
                                "duration_seconds": round(duration, 2),
                                "error": str(handler_error),
                            },
                        )
                        if self.storage is not None:
                            self.storage.insert_action_log(
                                job.site,
                                job.target.external_id,
                                "selenium_worker",
                                job.case,
                                result_status,
                                json.dumps(
                                    {
                                        "duration_seconds": round(duration, 2),
                                        "error": str(handler_error),
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                    else:
                        self.state.jobs_completed += 1
                        result_status = trace.result_status or "success"
                        self.state.last_result = result_status
                        self.state.last_error = None
                        if result_status in {"success", "purchase_confirmed"}:
                            trace.success(
                                "job finished",
                                {
                                    "duration_seconds": round(duration, 2),
                                    "url": self._safe_driver_url(),
                                    "result": result_status,
                                },
                            )
                        elif result_status == PURCHASE_STATUS_UNKNOWN_REVIEW:
                            trace.warning(
                                "Worker finished with unknown result; manual review required",
                                {
                                    "duration_seconds": round(duration, 2),
                                    "url": self._safe_driver_url(),
                                    "result": result_status,
                                },
                                level="minimal",
                            )
                        else:
                            trace.warning(
                                f"Worker finished with result={result_status}",
                                {
                                    "duration_seconds": round(duration, 2),
                                    "url": self._safe_driver_url(),
                                    "result": result_status,
                                },
                                level="minimal",
                            )
                        self._log_runtime(
                            "WARNING" if result_status == PURCHASE_STATUS_UNKNOWN_REVIEW else "INFO",
                            "success" if result_status in {"success", "purchase_confirmed"} else "action",
                            f"job finished case={job.case} external_id={job.target.external_id} status={result_status} duration={duration:.2f}s",
                            site=job.site,
                            details={
                                "case": job.case,
                                "external_id": job.target.external_id,
                                "status": result_status,
                                "duration_seconds": round(duration, 2),
                            },
                        )
                        if self.storage is not None:
                            self.storage.insert_action_log(
                                job.site,
                                job.target.external_id,
                                "selenium_worker",
                                job.case,
                                result_status,
                                json.dumps(
                                    {"duration_seconds": round(duration, 2)},
                                    ensure_ascii=False,
                                ),
                            )

                    controlled_result = trace.result_status in CONTROLLED_WORKER_RESULTS
                    should_rebuild = (
                        timed_out["value"]
                        or self.driver is None
                        or (
                            handler_error is not None
                            and not controlled_result
                            and not bool(getattr(self.cfg, "selenium_keep_browser_alive", True))
                        )
                    )
                    if should_rebuild and not self.stop_event.is_set():
                        trace.warning("Browser session will be rebuilt after action result", level="verbose")
                        try:
                            self.rebuild_driver()
                        except Exception as exc:
                            self.state.last_error = f"driver_reinit_failed: {type(exc).__name__}: {exc}"
                            self.driver = None
                            self._log_runtime(
                                "ERROR",
                                "error",
                                f"driver rebuild failed error={exc}",
                            )

                    if self.dispatcher is not None:
                        self.dispatcher.mark_finished(job)

                    if timed_out["value"] and not timeline.timeline_path:
                        try:
                            timeline.write(result="timed_out", reason=self.state.last_error or "job timed out")
                        except Exception:
                            logger.exception("[selenium] failed writing timeout timeline action_id=%s", trace.action_id)
                    elif handler_error is None and low_level_debug_enabled(self.cfg):
                        try:
                            timeline.write(result=self.state.last_result or "success")
                        except Exception:
                            logger.exception("[selenium] failed writing success timeline action_id=%s", trace.action_id)

                    trace.finish(self.state.last_result or "unknown")
                    self.state.busy = False
                    self._set_action_active("")
                    self.state.last_action_latency_seconds = round(duration, 2)
                    self.job_queue.task_done()

        finally:
            self.state.ready = False
            self.state.busy = False
            self.state.lifecycle_state = "stopping"
            self.close_driver(reason="worker_finally")
            self.state.started = False
            self.state.lifecycle_state = "stopped"
            self._release_active_worker()

            self._log_runtime("INFO", "selenium", f"worker stopped queue_size={self.job_queue.qsize()}")

    def shutdown(self) -> None:
        self._log_runtime("INFO", "selenium", "selenium_worker_stop_requested")
        self._log_runtime(
            "INFO",
            "selenium",
            "selenium_runtime_stop_started",
            details={
                "driver_exists": self.driver is not None,
                "chromedriver_pid": self.state.chromedriver_pid,
                "tracked_chrome_pids": list(getattr(self.state, "tracked_chrome_pids", []) or []),
            },
        )
        self.state.warm_refresh_paused_reason = "stop_requested"
        self._log_runtime(
            "INFO",
            "selenium",
            "selenium_warm_refresh_stop_requested",
            details={"warm_refresh_running": self.state.warm_refresh_running},
        )
        self.stop_event.set()
        self.close_driver(reason="shutdown")
