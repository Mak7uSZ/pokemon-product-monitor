from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

from pokemon_parser.models import SeleniumJob
from pokemon_parser.utils.time import utc_now_iso

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitResult:
    status: str  # queued | already_queued | already_running
    job_key: str


class SeleniumDispatcher:
    """
    Single entry point for Selenium jobs.
    Guarantees FIFO queueing and skips duplicate pending/running jobs.
    """

    def __init__(
        self,
        job_queue: "queue.Queue[SeleniumJob]",
        *,
        worker_factory: Callable[[], Any] | None = None,
    ):
        self.job_queue = job_queue
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._pending_keys: set[str] = set()
        self._running_keys: set[str] = set()
        self._pending_jobs: dict[str, SeleniumJob] = {}
        self._running_jobs: dict[str, SeleniumJob] = {}
        self._worker_factory = worker_factory
        self._worker: Any | None = None
        self._last_start_at: str | None = None
        self._last_stop_at: str | None = None
        self._last_error: str = ""
        self._lifecycle_state = "stopped"
        self._duplicate_start_ignored_count = 0
        self._duplicate_start_guard_count = 0
        self._config_snapshot: dict[str, Any] = {}
        self._last_prewarm_skip_reason = ""
        self._last_prewarm_error = ""

    @staticmethod
    def make_job_key(job: SeleniumJob) -> str:
        return f"{job.site}|{job.case}|{job.target.external_id}"

    @property
    def worker(self) -> Any | None:
        with self._lifecycle_lock:
            return self._worker

    @staticmethod
    def _worker_alive(worker: Any | None) -> bool:
        if worker is None or not hasattr(worker, "is_alive"):
            return False
        try:
            return bool(worker.is_alive())
        except Exception:
            return False

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._lifecycle_state in {"starting", "running"} or self._worker_alive(self._worker):
                self._duplicate_start_ignored_count += 1
                self._duplicate_start_guard_count += 1
                logger.info("selenium_dispatcher_start_ignored_already_running")
                worker_state = getattr(self._worker, "state", None)
                if getattr(worker_state, "duplicate_start_ignored_count", None) is not None:
                    try:
                        worker_state.duplicate_start_ignored_count += 1
                        if getattr(worker_state, "duplicate_start_guard_count", None) is not None:
                            worker_state.duplicate_start_guard_count += 1
                    except Exception:
                        pass
                return False

            if self._worker_factory is None:
                logger.info("[dispatcher] selenium worker factory unavailable; queued jobs remain pending")
                return False

            self._lifecycle_state = "starting"
            worker = self._worker_factory()
            self._worker = worker
            self._last_start_at = utc_now_iso()
            self._last_error = ""

            try:
                result = worker.start()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._lifecycle_state = "stopped"
                logger.exception("[dispatcher] selenium worker start failed")
                raise

            if result is False:
                self._last_error = "selenium worker start ignored"
                self._duplicate_start_ignored_count += 1
                self._duplicate_start_guard_count += 1
                self._lifecycle_state = "running" if self._worker_alive(worker) else "stopped"
                logger.info("selenium_dispatcher_start_ignored_already_running")
                return False

            self._lifecycle_state = "running"
            logger.info("[dispatcher] selenium worker start requested")
            return True

    def set_runtime_config(self, config: dict[str, Any], *, prewarm_skip_reason: str | None = None) -> None:
        with self._lifecycle_lock:
            self._config_snapshot = dict(config or {})
            self._last_prewarm_skip_reason = prewarm_skip_reason or ""

    def stop(self, *, timeout: float = 5.0) -> bool:
        with self._lifecycle_lock:
            worker = self._worker
            if worker is None:
                self._last_stop_at = utc_now_iso()
                self._lifecycle_state = "stopped"
                return True

            self._lifecycle_state = "stopping"
            if hasattr(worker, "shutdown"):
                try:
                    worker.shutdown()
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("[dispatcher] selenium worker shutdown failed")

            if hasattr(worker, "join"):
                try:
                    worker.join(timeout=timeout)
                except RuntimeError:
                    # Thread was never started because another owner was already active.
                    pass
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("[dispatcher] selenium worker join failed")

            stopped = not self._worker_alive(worker)
            self._last_stop_at = utc_now_iso()
            if not stopped:
                self._last_error = "selenium worker did not stop before timeout"
                self._lifecycle_state = "running"
                logger.error("[dispatcher] selenium worker still alive after timeout=%s", timeout)
            else:
                self._lifecycle_state = "stopped"
            return stopped

    def submit(self, job: SeleniumJob) -> SubmitResult:
        job_key = self.make_job_key(job)

        with self._lock:
            if job_key in self._running_keys:
                logger.warning(
                    "[dispatcher] duplicate running job skipped key=%s site=%s external_id=%s action_id=%s queue_size=%s",
                    job_key,
                    job.site,
                    job.target.external_id,
                    job.action_id,
                    self.job_queue.qsize(),
                )
                return SubmitResult(status="already_running", job_key=job_key)

            if job_key in self._pending_keys:
                logger.warning(
                    "[dispatcher] duplicate pending job skipped key=%s site=%s external_id=%s action_id=%s queue_size=%s",
                    job_key,
                    job.site,
                    job.target.external_id,
                    job.action_id,
                    self.job_queue.qsize(),
                )
                return SubmitResult(status="already_queued", job_key=job_key)

            self._pending_keys.add(job_key)
            self._pending_jobs[job_key] = job
            self.job_queue.put(job)
            logger.info(
                "[dispatcher] job queued key=%s site=%s external_id=%s action_id=%s target_url=%s queue_size=%s",
                job_key,
                job.site,
                job.target.external_id,
                job.action_id,
                job.target.product_url,
                self.job_queue.qsize(),
            )
            result = SubmitResult(status="queued", job_key=job_key)

        self.start()
        return result

    def mark_started(self, job: SeleniumJob) -> None:
        job_key = self.make_job_key(job)

        with self._lock:
            self._pending_keys.discard(job_key)
            self._pending_jobs.pop(job_key, None)
            self._running_keys.add(job_key)
            self._running_jobs[job_key] = job
            logger.info(
                "[dispatcher] job started key=%s site=%s external_id=%s action_id=%s pending=%s running=%s queue_size=%s",
                job_key,
                job.site,
                job.target.external_id,
                job.action_id,
                len(self._pending_keys),
                len(self._running_keys),
                self.job_queue.qsize(),
            )

    def mark_finished(self, job: SeleniumJob) -> None:
        job_key = self.make_job_key(job)

        with self._lock:
            self._running_keys.discard(job_key)
            self._running_jobs.pop(job_key, None)
            logger.info(
                "[dispatcher] job finished key=%s site=%s external_id=%s action_id=%s pending=%s running=%s queue_size=%s",
                job_key,
                job.site,
                job.target.external_id,
                job.action_id,
                len(self._pending_keys),
                len(self._running_keys),
                self.job_queue.qsize(),
            )

    def snapshot(self) -> dict[str, list[str]]:
        with self._lock:
            return {
                "pending": sorted(self._pending_keys),
                "running": sorted(self._running_keys),
            }

    @staticmethod
    def _job_snapshot(job_key: str, job: SeleniumJob | None) -> dict[str, Any]:
        return {
            "job_key": job_key,
            "site": job.site if job else None,
            "external_id": job.target.external_id if job else None,
            "title": job.target.title if job else None,
            "action_id": job.action_id if job else None,
            "created_at": job.created_at if job else None,
            "metadata": dict(job.metadata) if job else {},
        }

    def detailed_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return {
                "pending": [
                    self._job_snapshot(job_key, self._pending_jobs.get(job_key))
                    for job_key in sorted(self._pending_keys)
                ],
                "running": [
                    self._job_snapshot(job_key, self._running_jobs.get(job_key))
                    for job_key in sorted(self._running_keys)
                ],
            }

    def clear_pending(self, *, site: str | None = None) -> dict[str, Any]:
        removed: list[dict[str, Any]] = []
        kept: list[SeleniumJob] = []

        with self._lock:
            while True:
                try:
                    job = self.job_queue.get_nowait()
                except queue.Empty:
                    break

                job_key = self.make_job_key(job)
                should_remove = site is None or job.site == site
                if should_remove:
                    removed.append(self._job_snapshot(job_key, job))
                    self._pending_keys.discard(job_key)
                    self._pending_jobs.pop(job_key, None)
                else:
                    kept.append(job)

                try:
                    self.job_queue.task_done()
                except ValueError:
                    pass

            for job in kept:
                self.job_queue.put(job)

            if site is not None:
                stale_keys = [
                    job_key
                    for job_key, job in self._pending_jobs.items()
                    if job.site == site and all(item["job_key"] != job_key for item in removed)
                ]
            else:
                stale_keys = [
                    job_key
                    for job_key in self._pending_keys
                    if all(item["job_key"] != job_key for item in removed)
                    and job_key not in {self.make_job_key(job) for job in kept}
                ]

            for job_key in stale_keys:
                job = self._pending_jobs.pop(job_key, None)
                self._pending_keys.discard(job_key)
                removed.append(self._job_snapshot(job_key, job))

        return {
            "removed_pending": len(removed),
            "removed_jobs": removed,
            "site": site,
        }

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "pending": len(self._pending_keys),
                "running": len(self._running_keys),
                "total": len(self._pending_keys) + len(self._running_keys),
            }

    def configure_warm_tabs(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lifecycle_lock:
            worker = self._worker
        if worker is None or not hasattr(worker, "configure_warm_tabs"):
            return {"ok": False, "configured": 0, "reason": "worker_unavailable"}
        return worker.configure_warm_tabs(items)

    def preload_warm_tabs_now(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            worker = self._worker
        if worker is None or not hasattr(worker, "preload_warm_tabs_now"):
            return {"ok": False, "opened": 0, "reason": "worker_unavailable"}
        return worker.preload_warm_tabs_now()

    def prewarm_browser(self, *, reason: str = "runtime_start", wait_ready_timeout: float = 20.0) -> dict[str, Any]:
        self.start()
        with self._lifecycle_lock:
            worker = self._worker
        if worker is None or not hasattr(worker, "prewarm_browser"):
            self._last_prewarm_skip_reason = "worker_unavailable"
            return {"ok": False, "skipped": True, "reason": "worker_unavailable"}

        ready_event = getattr(worker, "ready_event", None)
        if ready_event is not None and hasattr(ready_event, "wait"):
            try:
                ready_event.wait(timeout=wait_ready_timeout)
            except Exception:
                pass

        result = worker.prewarm_browser(reason=reason)
        with self._lifecycle_lock:
            self._last_prewarm_skip_reason = str(result.get("reason") or "")
            if not result.get("ok") and not result.get("skipped"):
                self._last_prewarm_error = self._last_prewarm_skip_reason
        return result

    def lifecycle_snapshot(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            worker = self._worker
            last_start_at = self._last_start_at
            last_stop_at = self._last_stop_at
            last_error = self._last_error

        state = getattr(worker, "state", None) if worker is not None else None
        if worker is not None and hasattr(worker, "refresh_lifecycle_snapshot"):
            try:
                worker.refresh_lifecycle_snapshot()
            except Exception:
                logger.exception("[dispatcher] selenium lifecycle refresh failed")
            state = getattr(worker, "state", None)
        driver = getattr(worker, "driver", None) if worker is not None else None
        driver_session_id = getattr(state, "driver_session_id", "") if state is not None else ""
        if not driver_session_id and driver is not None:
            try:
                driver_session_id = str(getattr(driver, "session_id", "") or "")
            except Exception:
                driver_session_id = ""
        selenium_window_count = (
            worker.selenium_window_count()
            if worker is not None and hasattr(worker, "selenium_window_count")
            else getattr(state, "selenium_window_count", None) if state is not None else 0
        )

        snapshot: dict[str, Any] = {
            "dispatcher_exists": True,
            "dispatcher_running": self._worker_alive(worker),
            "worker_thread_alive": self._worker_alive(worker),
            "lifecycle_state": getattr(state, "lifecycle_state", self._lifecycle_state) if state is not None else self._lifecycle_state,
            "driver_exists": driver is not None,
            "driver_session_id": driver_session_id,
            "browser_started_at": getattr(state, "browser_started_at", None) if state is not None else None,
            "last_driver_create_at": getattr(state, "last_driver_create_at", None) if state is not None else None,
            "last_driver_quit_at": getattr(state, "last_driver_quit_at", None) if state is not None else None,
            "chromedriver_pid": getattr(state, "chromedriver_pid", None) if state is not None else None,
            "chrome_pid": getattr(state, "chrome_pid", None) if state is not None else None,
            "tracked_chrome_pids": list(getattr(state, "tracked_chrome_pids", []) or []) if state is not None else [],
            "orphan_app_chrome_pids": list(getattr(state, "orphan_app_chrome_pids", []) or []) if state is not None else [],
            "selenium_window_count": selenium_window_count,
            "selenium_top_level_window_ids": (
                list(getattr(state, "selenium_top_level_window_ids", []) or []) if state is not None else []
            ),
            "selenium_top_level_window_count": (
                getattr(state, "selenium_top_level_window_count", None) if state is not None else None
            ),
            "selenium_top_level_window_id_by_handle": (
                dict(getattr(state, "selenium_top_level_window_id_by_handle", {}) or {}) if state is not None else {}
            ),
            "window_handles_count": int(getattr(state, "window_handles_count", 0) or 0) if state is not None else 0,
            "window_handles_current_urls": (
                dict(getattr(state, "window_handles_current_urls", {}) or {}) if state is not None else {}
            ),
            "last_window_snapshot_at": getattr(state, "last_window_snapshot_at", None) if state is not None else None,
            "last_start_at": getattr(state, "last_start_at", None) if state is not None else last_start_at,
            "last_stop_at": getattr(state, "last_stop_at", None) if state is not None else last_stop_at,
            "last_error": getattr(state, "last_error", "") if state is not None else last_error,
            "started": bool(getattr(state, "started", False)) if state is not None else False,
            "ready": bool(getattr(state, "ready", False)) if state is not None else False,
            "busy": bool(getattr(state, "busy", False)) if state is not None else False,
            "last_job": getattr(state, "last_job", "") if state is not None else "",
            "last_result": getattr(state, "last_result", "") if state is not None else "",
            "last_duration_seconds": getattr(state, "last_duration_seconds", 0.0) if state is not None else 0.0,
            "jobs_completed": getattr(state, "jobs_completed", 0) if state is not None else 0,
            "jobs_failed": getattr(state, "jobs_failed", 0) if state is not None else 0,
            "jobs_timed_out": getattr(state, "jobs_timed_out", 0) if state is not None else 0,
            "driver_rebuilds": getattr(state, "driver_rebuilds", 0) if state is not None else 0,
            "last_diagnostic_snapshot": getattr(state, "last_diagnostic_snapshot", "") if state is not None else "",
            "config": (getattr(state, "config", None) or self._config_snapshot) if state is not None else self._config_snapshot,
            "prewarmed": bool(getattr(state, "prewarmed", False)) if state is not None else False,
            "last_prewarm_skip_reason": (
                getattr(state, "last_prewarm_skip_reason", self._last_prewarm_skip_reason)
                if state is not None
                else self._last_prewarm_skip_reason
            ),
            "last_prewarm_error": (
                getattr(state, "last_prewarm_error", self._last_prewarm_error)
                if state is not None
                else self._last_prewarm_error
            ),
            "warm_tabs_enabled": bool(getattr(state, "warm_tabs_enabled", False)) if state is not None else False,
            "warm_tabs_count": int(getattr(state, "warm_tabs_count", 0) or 0) if state is not None else 0,
            "warm_tabs_max": int(getattr(state, "warm_tabs_max", 0) or 0) if state is not None else 0,
            "warm_tabs": worker.warm_tabs_snapshot() if worker is not None and hasattr(worker, "warm_tabs_snapshot") else [],
            "warm_tab_urls": list(getattr(state, "warm_tab_urls", []) or []) if state is not None else [],
            "active_action": getattr(state, "active_action", "") if state is not None else "",
            "active_worker_action": getattr(state, "active_worker_action", "") if state is not None else "",
            "worker_busy": bool(getattr(state, "busy", False)) if state is not None else False,
            "warm_refresh_running": bool(getattr(state, "warm_refresh_running", False)) if state is not None else False,
            "warm_refresh_paused_reason": getattr(state, "warm_refresh_paused_reason", "") if state is not None else "",
            "challenge_detected_count": int(getattr(state, "challenge_detected_count", 0) or 0) if state is not None else 0,
            "challenge_sources": dict(getattr(state, "challenge_sources", {}) or {}) if state is not None else {},
            "challenge_manual_action_required": (
                bool(getattr(state, "challenge_manual_action_required", False)) if state is not None else False
            ),
            "last_action_latency_seconds": getattr(state, "last_action_latency_seconds", 0.0) if state is not None else 0.0,
            "last_button_search_latency_seconds": getattr(state, "last_button_search_latency_seconds", 0.0) if state is not None else 0.0,
            "duplicate_start_ignored_count": (
                getattr(state, "duplicate_start_ignored_count", self._duplicate_start_ignored_count)
                if state is not None
                else self._duplicate_start_ignored_count
            ),
            "duplicate_start_guard_count": (
                getattr(state, "duplicate_start_guard_count", self._duplicate_start_guard_count)
                if state is not None
                else self._duplicate_start_guard_count
            ),
        }
        snapshot.update(self.counts())
        return snapshot
