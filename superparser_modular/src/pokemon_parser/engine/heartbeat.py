from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from pokemon_parser.utils.proxy import build_aiohttp_proxy_kwargs

logger = logging.getLogger(__name__)


def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _format_seconds(value: Any) -> str:
    try:
        if value is None:
            return "-"
        value = float(value)
        if value <= 0:
            return "-"
        if value < 60:
            return f"{value:.1f}s"
        minutes = int(value // 60)
        seconds = int(value % 60)
        if minutes < 60:
            return f"{minutes}m {seconds}s"
        hours = minutes // 60
        minutes = minutes % 60
        return f"{hours}h {minutes}m"
    except Exception:
        return "-"


def _format_iso_ago(value: str | None) -> str:
    if not value:
        return "-"

    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return _format_seconds(max(0.0, delta.total_seconds())) + " ago"
    except Exception:
        return value


def _format_breaker(value: Any) -> str:
    if value is None:
        return "unknown"

    if hasattr(value, "value"):
        try:
            return str(value.value).lower()
        except Exception:
            pass

    text = str(value)
    if "." in text:
        text = text.split(".")[-1]
    return text.lower()


def _build_selenium_snapshot(selenium_state: Any, selenium_dispatcher: Any = None) -> dict[str, Any] | None:
    if selenium_dispatcher is not None and hasattr(selenium_dispatcher, "lifecycle_snapshot"):
        return selenium_dispatcher.lifecycle_snapshot()

    if selenium_state is None:
        return None

    return {
        "started": bool(_safe_getattr(selenium_state, "started", False)),
        "ready": bool(_safe_getattr(selenium_state, "ready", False)),
        "busy": bool(_safe_getattr(selenium_state, "busy", False)),
        "last_error": _safe_getattr(selenium_state, "last_error", ""),
        "last_job": _safe_getattr(selenium_state, "last_job", ""),
        "last_result": _safe_getattr(selenium_state, "last_result", ""),
        "last_duration_seconds": _safe_getattr(selenium_state, "last_duration_seconds", 0.0),
        "jobs_completed": _safe_getattr(selenium_state, "jobs_completed", 0),
        "jobs_failed": _safe_getattr(selenium_state, "jobs_failed", 0),
        "jobs_timed_out": _safe_getattr(selenium_state, "jobs_timed_out", 0),
        "driver_rebuilds": _safe_getattr(selenium_state, "driver_rebuilds", 0),
    }


def build_heartbeat_text(
    *,
    runtime_overview: dict[str, Any],
    antiban: Any,
) -> str:
    site_states = runtime_overview.get("site_states", {})
    selenium = runtime_overview.get("selenium") or {}
    active_parsers = runtime_overview.get("active_parsers") or []
    enabled_parsers = runtime_overview.get("enabled_parsers") or []
    queue_size = runtime_overview.get("queue_size", 0)
    watchlist = runtime_overview.get("watchlist") or {}
    antiban_snapshot = antiban.snapshot() if antiban is not None and hasattr(antiban, "snapshot") else {}

    lines: list[str] = []
    lines.append("<b>Heartbeat</b>")
    lines.append(
        "runtime: "
        f"running={runtime_overview.get('running', False)} | "
        f"mode={html.escape(str(runtime_overview.get('action_mode', 'unknown')))} | "
        f"active={len(active_parsers)} | "
        f"enabled={len(enabled_parsers)} | "
        f"queue={queue_size} | "
        f"concurrency={runtime_overview.get('scan_concurrency', '-')}"
    )
    lines.append("")

    if watchlist:
        cooldowns = watchlist.get("cooldowns") or {}
        active_cooldowns = {
            site: seconds
            for site, seconds in cooldowns.items()
            if float(seconds or 0) > 0
        }
        cooldown_text = " ".join(
            f"{html.escape(str(site))}_backoff={_format_seconds(seconds)}"
            for site, seconds in active_cooldowns.items()
        ) or "backoff=-"
        lines.append("<b>Priority Watchlist</b>")
        lines.append(
            "watchlist: "
            f"enabled={str(bool(watchlist.get('enabled'))).lower()} | "
            f"running={str(bool(watchlist.get('running'))).lower()} | "
            f"last_checked={watchlist.get('last_cycle_checked_count', 0)} | "
            f"changed={watchlist.get('last_cycle_changed_count', 0)} | "
            f"errors={watchlist.get('last_cycle_error_count', 0)} | "
            f"next={_format_seconds(watchlist.get('next_cycle_in_seconds'))} | "
            f"enabled_items={watchlist.get('total_enabled_watchlist_items', 0)} | "
            f"{cooldown_text}"
        )
        if watchlist.get("last_error"):
            lines.append(f"watchlist_error={html.escape(str(watchlist.get('last_error')))}")
        lines.append("")

    for site in site_states:
        state = site_states[site]
        antiban_site = antiban_snapshot.get(site, {}) if isinstance(antiban_snapshot, dict) else {}
        parser_antiban = antiban_site.get("parser", {}) if isinstance(antiban_site, dict) else {}
        worker_antiban = antiban_site.get("worker", {}) if isinstance(antiban_site, dict) else {}

        lines.append(f"<b>{html.escape(str(state.get('label', site)))}</b>")
        lines.append(
            "state: "
            f"enabled={'yes' if state.get('enabled') else 'no'} | "
            f"status={html.escape(str(state.get('status', 'unknown')))} | "
            f"next={_format_seconds(state.get('next_in_seconds'))} | "
            f"cooldown={_format_seconds(state.get('cooldown_in_seconds'))} | "
            f"last_run={_format_iso_ago(state.get('last_run_at'))}"
        )
        lines.append(
            "result: "
            f"last_success={_format_iso_ago(state.get('last_success_at'))} | "
            f"last_error={_format_iso_ago(state.get('last_error_at'))} | "
            f"items={state.get('last_items_found', 0)} | "
            f"events={state.get('last_events_found', 0)}"
        )
        lines.append(
            "antiban: "
            f"parser={_format_breaker(parser_antiban.get('breaker'))} | "
            f"worker={_format_breaker(worker_antiban.get('breaker'))} | "
            f"parser_cooldown={_format_seconds(max(0.0, float(parser_antiban.get('cooldown_until', 0.0) or 0.0) - time.monotonic())) if parser_antiban else '-'} | "
            f"worker_cooldown={_format_seconds(max(0.0, float(worker_antiban.get('cooldown_until', 0.0) or 0.0) - time.monotonic())) if worker_antiban else '-'}"
        )
        last_error = state.get("last_error") or "-"
        lines.append(f"message: {html.escape(str(state.get('message') or '-'))}")
        lines.append(f"error: {html.escape(str(last_error))}")
        lines.append("")

    if selenium:
        lines.append("<b>Selenium</b>")
        lines.append(
            f"ready={selenium.get('ready', False)} | "
            f"busy={selenium.get('busy', False)} | "
            f"worker_alive={selenium.get('worker_thread_alive', False)} | "
            f"driver={selenium.get('driver_exists', False)} | "
            f"completed={selenium.get('jobs_completed', 0)} | "
            f"failed={selenium.get('jobs_failed', 0)} | "
            f"timed_out={selenium.get('jobs_timed_out', 0)} | "
            f"rebuilds={selenium.get('driver_rebuilds', 0)}"
        )
        lines.append(
            f"last_result={html.escape(str(selenium.get('last_result') or '-'))} | "
            f"last_job={html.escape(str(selenium.get('last_job') or '-'))} | "
            f"last_duration={_format_seconds(selenium.get('last_duration_seconds'))}"
        )

    return "\n".join(lines)


async def _send_telegram_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    proxy_cfg: Any = None,
) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, **build_aiohttp_proxy_kwargs(proxy_cfg)) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(
                    f"telegram send failed status={resp.status} body={body[:500]}"
                )


async def heartbeat_loop(
    *,
    cfg: Any,
    antiban: Any,
    selenium_state: Any,
    selenium_dispatcher: Any = None,
    runtime_state: Any = None,
    watchlist_state: Any = None,
    storage: Any = None,
    interval_seconds: int = 3600,
) -> None:
    bot_token = _safe_getattr(cfg, "telegram_bot_token", None)
    chat_id = _safe_getattr(cfg, "telegram_chat_id", None)

    logger.info("[heartbeat] started interval_seconds=%s", interval_seconds)

    while True:
        try:
            dispatcher_counts = selenium_dispatcher.counts() if selenium_dispatcher is not None else {"total": 0}
            selenium_snapshot = _build_selenium_snapshot(selenium_state, selenium_dispatcher)

            if runtime_state is not None and hasattr(runtime_state, "snapshot_overview"):
                runtime_overview = runtime_state.snapshot_overview(
                    queue_size=dispatcher_counts.get("total", 0),
                    selenium=selenium_snapshot,
                )
                if watchlist_state is not None and hasattr(watchlist_state, "snapshot"):
                    runtime_overview["watchlist"] = watchlist_state.snapshot(storage=storage)
                runtime_state.mark_heartbeat()
            else:
                runtime_overview = {
                    "running": False,
                    "action_mode": _safe_getattr(cfg, "action_mode", "unknown"),
                    "queue_size": dispatcher_counts.get("total", 0),
                    "scan_concurrency": _safe_getattr(cfg, "parser_concurrency", 1),
                    "active_parsers": [],
                    "enabled_parsers": [],
                    "site_states": {},
                    "selenium": selenium_snapshot,
                }
                if watchlist_state is not None and hasattr(watchlist_state, "snapshot"):
                    runtime_overview["watchlist"] = watchlist_state.snapshot(storage=storage)

            text = build_heartbeat_text(
                runtime_overview=runtime_overview,
                antiban=antiban,
            )

            if storage is not None and hasattr(storage, "insert_runtime_log"):
                storage.insert_runtime_log(
                    level="INFO",
                    category="heartbeat",
                    message=(
                        f"heartbeat queue={runtime_overview.get('queue_size', 0)} "
                        f"active={len(runtime_overview.get('active_parsers') or [])} "
                        f"watchlist_enabled={bool((runtime_overview.get('watchlist') or {}).get('enabled'))} "
                        f"watchlist_running={bool((runtime_overview.get('watchlist') or {}).get('running'))} "
                        f"watchlist_checked={(runtime_overview.get('watchlist') or {}).get('last_cycle_checked_count', 0)} "
                        f"watchlist_errors={(runtime_overview.get('watchlist') or {}).get('last_cycle_error_count', 0)}"
                    ),
                    details=runtime_overview,
                )

            notifications_enabled = bool(_safe_getattr(cfg, "enable_notifications", True))
            heartbeat_enabled = bool(_safe_getattr(cfg, "enable_heartbeat_alerts", True))

            if notifications_enabled and heartbeat_enabled and bot_token and chat_id:
                await _send_telegram_message(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    text=text,
                    proxy_cfg=cfg,
                )
                logger.info("[heartbeat] telegram sent")
            else:
                logger.info("[heartbeat] tick recorded without telegram delivery")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[heartbeat] tick failed")

        await asyncio.sleep(interval_seconds)
