from __future__ import annotations

import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal

from pokemon_parser.utils.time import utc_now_iso

RunStatus = Literal["success", "partial_success", "recovered_via_fallback", "error", "skipped"]


@dataclass(frozen=True)
class SiteRunResult:
    site: str
    status: RunStatus
    items_found: int = 0
    events_found: int = 0
    message: str | None = None


@dataclass
class SiteRuntimeState:
    site: str
    label: str
    enabled: bool = True
    status: str = "idle"
    active: bool = False
    message: str = ""
    last_state_change_at: str | None = None
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error: str = ""
    last_items_found: int = 0
    last_events_found: int = 0
    next_in_seconds: float = 0.0
    cooldown_in_seconds: float = 0.0
    runs_started: int = 0
    runs_completed: int = 0
    successes: int = 0
    failures: int = 0
    skips: int = 0


class RuntimeStateStore:
    def __init__(
        self,
        *,
        site_labels: dict[str, str],
        enabled_map: dict[str, bool],
        action_mode: str,
        scan_concurrency: int,
    ) -> None:
        self._lock = threading.Lock()
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._last_heartbeat_at: str | None = None
        self._running = False
        self._action_mode = action_mode
        self._scan_concurrency = scan_concurrency
        self._sites = {
            site: SiteRuntimeState(
                site=site,
                label=site_labels.get(site, site.title()),
                enabled=bool(enabled_map.get(site, True)),
                status="idle" if enabled_map.get(site, True) else "disabled",
                last_state_change_at=utc_now_iso(),
            )
            for site in site_labels
        }

    def set_enabled_map(self, enabled_map: dict[str, bool]) -> None:
        now = utc_now_iso()
        with self._lock:
            for site, state in self._sites.items():
                enabled = bool(enabled_map.get(site, False))
                state.enabled = enabled
                if not enabled:
                    state.active = False
                    state.status = "disabled"
                    state.message = "parser disabled"
                    state.next_in_seconds = 0.0
                    state.cooldown_in_seconds = 0.0
                    state.last_state_change_at = now
                elif state.status == "disabled":
                    state.status = "idle"
                    state.message = ""
                    state.last_state_change_at = now

    def mark_runtime_started(self) -> None:
        now = utc_now_iso()
        with self._lock:
            self._running = True
            self._started_at = now
            self._stopped_at = None

    def mark_runtime_stopped(self, message: str = "") -> None:
        now = utc_now_iso()
        with self._lock:
            self._running = False
            self._stopped_at = now
            for state in self._sites.values():
                if not state.enabled:
                    state.status = "disabled"
                elif message:
                    state.status = "idle"
                    state.message = message
                else:
                    state.status = "idle"
                state.active = False
                state.next_in_seconds = 0.0
                state.cooldown_in_seconds = 0.0
                state.last_state_change_at = now

    def mark_waiting(
        self,
        site: str,
        *,
        next_in_seconds: float,
        cooldown_in_seconds: float = 0.0,
        message: str = "",
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            state = self._sites[site]
            if not state.enabled:
                state.status = "disabled"
                state.active = False
                state.next_in_seconds = 0.0
                state.cooldown_in_seconds = 0.0
                state.message = "parser disabled"
                state.last_state_change_at = now
                return

            state.active = False
            state.next_in_seconds = max(0.0, round(float(next_in_seconds), 3))
            state.cooldown_in_seconds = max(0.0, round(float(cooldown_in_seconds), 3))
            state.status = "cooldown" if state.cooldown_in_seconds > 0 else "idle"
            state.message = message
            state.last_state_change_at = now

    def mark_scan_started(self, site: str) -> None:
        now = utc_now_iso()
        with self._lock:
            state = self._sites[site]
            state.active = True
            state.status = "scanning"
            state.message = "scan in progress"
            state.last_run_at = now
            state.last_state_change_at = now
            state.next_in_seconds = 0.0
            state.cooldown_in_seconds = 0.0
            state.runs_started += 1

    def mark_scan_result(
        self,
        site: str,
        result: SiteRunResult,
        *,
        next_in_seconds: float,
        cooldown_in_seconds: float = 0.0,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            state = self._sites[site]
            state.active = False
            state.runs_completed += 1
            state.next_in_seconds = max(0.0, round(float(next_in_seconds), 3))
            state.cooldown_in_seconds = max(0.0, round(float(cooldown_in_seconds), 3))
            state.last_items_found = max(0, int(result.items_found))
            state.last_events_found = max(0, int(result.events_found))
            state.last_state_change_at = now

            if result.status in {"success", "partial_success", "recovered_via_fallback"}:
                state.successes += 1
                state.status = "cooldown" if state.cooldown_in_seconds > 0 else result.status
                state.message = result.message or f"items={result.items_found} events={result.events_found}"
                state.last_success_at = now
                state.last_error = ""
            elif result.status == "skipped":
                state.skips += 1
                state.status = "cooldown" if state.cooldown_in_seconds > 0 else "idle"
                state.message = result.message or "scan skipped"
            else:
                state.failures += 1
                state.status = "cooldown" if state.cooldown_in_seconds > 0 else "error"
                state.message = result.message or "scan failed"
                state.last_error = result.message or "scan failed"
                state.last_error_at = now

    def mark_heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat_at = utc_now_iso()

    def snapshot_sites(self) -> dict[str, dict]:
        with self._lock:
            return {
                site: asdict(state)
                for site, state in self._sites.items()
            }

    def snapshot_overview(
        self,
        *,
        queue_size: int = 0,
        selenium: dict | None = None,
    ) -> dict:
        with self._lock:
            site_payload = {
                site: asdict(state)
                for site, state in self._sites.items()
            }
            active_sites = [site for site, state in self._sites.items() if state.active]
            enabled_sites = [site for site, state in self._sites.items() if state.enabled]
            return {
                "running": self._running,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_heartbeat_at": self._last_heartbeat_at,
                "action_mode": self._action_mode,
                "scan_concurrency": self._scan_concurrency,
                "queue_size": max(0, int(queue_size)),
                "active_parsers": active_sites,
                "enabled_parsers": enabled_sites,
                "site_states": deepcopy(site_payload),
                "selenium": deepcopy(selenium) if selenium is not None else None,
            }


class WatchlistRuntimeState:
    def __init__(self, *, cfg, site_labels: dict[str, str]) -> None:
        self._lock = threading.Lock()
        self._cfg = cfg
        self._site_labels = dict(site_labels)
        self._enabled = bool(cfg.watchlist_enabled())
        self._running = False
        self._loop_started_at: str | None = None
        self._loop_stopped_at: str | None = None
        self._last_cycle_started_at: str | None = None
        self._last_cycle_finished_at: str | None = None
        self._last_cycle_duration_seconds = 0.0
        self._last_cycle_checked_count = 0
        self._last_cycle_changed_count = 0
        self._last_cycle_error_count = 0
        self._last_cycle_skipped_count = 0
        self._last_error = ""
        self._next_cycle_at_epoch: float | None = None
        self._site_cooldown_until_epoch: dict[str, float] = {}

    def _intervals(self) -> dict[str, float]:
        return {
            site: round(float(self._cfg.watchlist_interval_seconds(site)), 3)
            for site in self._site_labels
        }

    def mark_loop_started(self) -> None:
        with self._lock:
            self._enabled = bool(self._cfg.watchlist_enabled())
            self._running = self._enabled
            self._loop_started_at = utc_now_iso()
            self._loop_stopped_at = None
            if not self._enabled:
                self._next_cycle_at_epoch = None

    def mark_loop_stopped(self, error: str | None = None) -> None:
        with self._lock:
            self._running = False
            self._loop_stopped_at = utc_now_iso()
            self._next_cycle_at_epoch = None
            if error:
                self._last_error = str(error)

    def mark_disabled(self) -> None:
        with self._lock:
            self._enabled = False
            self._running = False
            self._next_cycle_at_epoch = None

    def mark_cycle_started(self) -> None:
        with self._lock:
            self._enabled = bool(self._cfg.watchlist_enabled())
            self._last_cycle_started_at = utc_now_iso()
            self._last_error = ""

    def mark_cycle_finished(
        self,
        *,
        duration_seconds: float,
        checked_count: int,
        changed_count: int,
        error_count: int,
        skipped_count: int,
        next_cycle_in_seconds: float,
    ) -> None:
        with self._lock:
            self._last_cycle_finished_at = utc_now_iso()
            self._last_cycle_duration_seconds = max(0.0, round(float(duration_seconds), 3))
            self._last_cycle_checked_count = max(0, int(checked_count))
            self._last_cycle_changed_count = max(0, int(changed_count))
            self._last_cycle_error_count = max(0, int(error_count))
            self._last_cycle_skipped_count = max(0, int(skipped_count))
            self._next_cycle_at_epoch = time.time() + max(0.0, float(next_cycle_in_seconds))

    def mark_cycle_failed(
        self,
        *,
        error: str,
        duration_seconds: float,
        next_cycle_in_seconds: float,
    ) -> None:
        with self._lock:
            self._last_cycle_finished_at = utc_now_iso()
            self._last_cycle_duration_seconds = max(0.0, round(float(duration_seconds), 3))
            self._last_cycle_error_count += 1
            self._last_error = str(error)
            self._next_cycle_at_epoch = time.time() + max(0.0, float(next_cycle_in_seconds))

    def mark_site_backoff(self, site: str, seconds: float) -> None:
        with self._lock:
            self._site_cooldown_until_epoch[site] = time.time() + max(0.0, float(seconds))

    def _counts_from_storage(self, storage) -> tuple[int, int, dict[str, int]]:
        if storage is None or not hasattr(storage, "watchlist_summary"):
            return 0, 0, {site: 0 for site in self._site_labels}
        try:
            summary = storage.watchlist_summary()
        except Exception:
            return 0, 0, {site: 0 for site in self._site_labels}
        sites = summary.get("sites", {}) if isinstance(summary, dict) else {}
        by_site = {
            site: int((sites.get(site) or {}).get("enabled", 0))
            for site in self._site_labels
        }
        return int(summary.get("total", 0) or 0), int(summary.get("enabled", 0) or 0), by_site

    def snapshot(self, *, storage=None) -> dict:
        total_items, total_enabled, enabled_by_site = self._counts_from_storage(storage)
        site_diagnostics = {}
        if storage is not None and hasattr(storage, "watchlist_site_diagnostics"):
            try:
                site_diagnostics = storage.watchlist_site_diagnostics()
            except Exception:
                site_diagnostics = {}
        now_epoch = time.time()
        with self._lock:
            cooldowns = {
                site: round(max(0.0, until_epoch - now_epoch), 3)
                for site, until_epoch in self._site_cooldown_until_epoch.items()
            }
            for site in self._site_labels:
                cooldowns.setdefault(site, 0.0)

            next_cycle_in = (
                max(0.0, self._next_cycle_at_epoch - now_epoch)
                if self._next_cycle_at_epoch is not None
                else None
            )
            next_cycle_estimate = (
                datetime.fromtimestamp(self._next_cycle_at_epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                if self._next_cycle_at_epoch is not None
                else None
            )
            return {
                "enabled": self._enabled,
                "running": self._running,
                "loop_started_at": self._loop_started_at,
                "loop_stopped_at": self._loop_stopped_at,
                "last_cycle_started_at": self._last_cycle_started_at,
                "last_cycle_finished_at": self._last_cycle_finished_at,
                "last_cycle_duration_seconds": self._last_cycle_duration_seconds,
                "last_cycle_checked_count": self._last_cycle_checked_count,
                "last_cycle_changed_count": self._last_cycle_changed_count,
                "last_cycle_error_count": self._last_cycle_error_count,
                "last_cycle_skipped_count": self._last_cycle_skipped_count,
                "last_error": self._last_error,
                "next_cycle_estimate": next_cycle_estimate,
                "next_cycle_in_seconds": round(next_cycle_in, 3) if next_cycle_in is not None else None,
                "intervals": self._intervals(),
                "cooldowns": cooldowns,
                "total_watchlist_items": total_items,
                "total_enabled_watchlist_items": total_enabled,
                "actively_monitored_watchlist_items": total_enabled if self._enabled else 0,
                "total_watchlist_items_by_site": enabled_by_site,
                "site_diagnostics": site_diagnostics,
                "mediamarkt_diagnostics": site_diagnostics.get("mediamarkt", {}),
            }
