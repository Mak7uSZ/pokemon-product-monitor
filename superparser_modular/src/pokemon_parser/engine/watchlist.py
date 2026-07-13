from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

from pokemon_parser.config import AppConfig
from pokemon_parser.filters.engine import match, match_precheck
from pokemon_parser.models import (
    ActionTarget,
    AddToCartTarget,
    CheckoutTarget,
    ParsedItem,
    SeleniumJob,
    WatchlistCheckResult,
)
from pokemon_parser.notifications.telegram import TelegramNotifier
from pokemon_parser.parsers import build_enabled_parser_registry
from pokemon_parser.parsers.bol import BolParser
from pokemon_parser.parsers.dreamland import DreamLandParser, DreamLandParserDeny
from pokemon_parser.parsers.mediamarkt import MediaMarktParser, MediaMarktParserDeny
from pokemon_parser.parsers.pocketgames import PocketGamesParser
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.utils.text import clean_text, normalize_text, parse_price_eur
from pokemon_parser.utils.time import utc_now_iso
from pokemon_parser.utils.url_safety import validate_retailer_url
from pokemon_parser.workers.purchase_safety import purchase_key_for_target

logger = logging.getLogger(__name__)
watchlist_logger = logging.getLogger("pokemon_parser.watchlist_tracker")
watchlist_decision_logger = logging.getLogger("pokemon_parser.watchlist_decisions")

AVAILABLE_STATUSES = {
    "in_stock",
    "add_to_cart_available",
    "delivery_available",
    "offer_available",
    "variant_available",
}


class WatchlistTracker:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        storage: SqliteStorage,
        notifier: TelegramNotifier | None = None,
        selenium_dispatcher=None,
        runtime_state=None,
    ) -> None:
        self.cfg = cfg
        self.storage = storage
        self.notifier = notifier
        self.selenium_dispatcher = selenium_dispatcher
        self.runtime_state = runtime_state
        self._cooldown_until: dict[str, float] = {}
        self._mediamarkt_artifacts_saved = 0
        self._mediamarkt_artifact_limit_logged = False
        self.parsers = {
            "mediamarkt": MediaMarktParser(),
            "dreamland": DreamLandParser(),
            "bol": BolParser(),
            "pocketgames": PocketGamesParser(),
        }

    def _watchlist_site_enabled(self, site: str) -> bool:
        checker = getattr(self.cfg, "watchlist_site_enabled", None)
        return bool(checker(site)) if checker else True

    def _log_lifecycle(self, level: str, message: str, details: dict[str, Any] | None = None) -> None:
        log_method = getattr(watchlist_logger, level.lower(), watchlist_logger.info)
        log_method("%s %s", message, json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")))
        try:
            self.storage.insert_runtime_log(
                level=level,
                category="watchlist",
                message=message,
                details=details or {},
            )
        except Exception:
            logger.exception("[watchlist] failed to persist lifecycle log message=%s", message)

    def _cycle_delay_seconds(self) -> float:
        enabled_sites = [
            site
            for site in self.cfg.enabled_parser_sites()
            if site in self.parsers and self._watchlist_site_enabled(site)
        ]
        if not enabled_sites:
            return 30.0
        return max(1.0, min(self.cfg.watchlist_interval_seconds(site) for site in enabled_sites))

    async def build_from_filters(
        self,
        session,
        *,
        site: str | None = None,
    ) -> dict[str, Any]:
        filters = self.storage.load_filters()
        parsers = build_enabled_parser_registry(self.cfg)
        if site:
            parsers = {site: parsers[site]} if site in parsers else {}
        parsers = {
            parser_site: parser
            for parser_site, parser in parsers.items()
            if self._watchlist_site_enabled(parser_site)
        }

        stats = {"ok": True, "sites": {}, "added_or_updated": 0, "matched": 0, "errors": []}
        for parser_site, parser in parsers.items():
            try:
                items = await parser.fetch(session, self.cfg)
            except Exception as exc:
                stats["errors"].append({"site": parser_site, "error": f"{type(exc).__name__}: {exc}"})
                continue

            site_stats = {"parsed": len(items), "matched": 0, "upserted": 0}
            for item in items:
                final_item = item
                if item.site == "bol" and isinstance(parser, BolParser):
                    try:
                        final_item = await parser.enrich(session, item, self.cfg)
                    except Exception:
                        final_item = item
                prematched = [rule for rule in filters if match_precheck(final_item, rule)]
                matched = [rule for rule in prematched if match(final_item, rule)]
                if not matched:
                    continue
                self.storage.upsert_watchlist_from_item(
                    final_item,
                    matched,
                    source="auto_filter_match",
                )
                site_stats["matched"] += 1
                site_stats["upserted"] += 1
                stats["matched"] += 1
                stats["added_or_updated"] += 1
            stats["sites"][parser_site] = site_stats
        return stats

    async def scan_once(
        self,
        session,
        *,
        site: str | None = None,
        product_key: str | None = None,
        item_id: int | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        next_delay = self._cycle_delay_seconds()
        if not self.cfg.watchlist_enabled():
            if self.runtime_state is not None:
                self.runtime_state.mark_disabled()
            self._log_lifecycle(
                "INFO",
                "watchlist_disabled",
                {"enabled": False, "next_cycle_in_seconds": None},
            )
            return {"ok": False, "message": "watchlist disabled", "checked": 0, "results": []}

        if self.runtime_state is not None:
            self.runtime_state.mark_cycle_started()
        self._log_lifecycle(
            "INFO",
            "watchlist_cycle_started",
            {"site": site, "product_key": product_key, "item_id": item_id},
        )

        try:
            if item_id is not None:
                item = self.storage.get_watchlist_item(item_id)
                items = [item] if item else []
            else:
                items = self.storage.list_watchlist(site=site, enabled=True, limit=2000)
                if product_key:
                    items = [item for item in items if str(item.get("product_key")) == str(product_key)]
            items = [item for item in items if self._watchlist_site_enabled(str(item.get("site") or ""))]
            if (
                self.selenium_dispatcher is not None
                and hasattr(self.selenium_dispatcher, "configure_warm_tabs")
            ):
                try:
                    # A targeted scan must not replace the worker's complete
                    # desired warm-tab registry with the one scanned item.
                    warm_items = self.storage.list_watchlist(enabled=True, limit=2000)
                    self.selenium_dispatcher.configure_warm_tabs(warm_items)
                except Exception:
                    logger.exception("[watchlist] failed to configure Selenium warm tabs")

            if not items:
                duration = time.monotonic() - started
                if self.runtime_state is not None:
                    self.runtime_state.mark_cycle_finished(
                        duration_seconds=duration,
                        checked_count=0,
                        changed_count=0,
                        error_count=0,
                        skipped_count=0,
                        next_cycle_in_seconds=next_delay,
                    )
                self._log_lifecycle(
                    "INFO",
                    "watchlist_no_items",
                    {
                        "site": site,
                        "product_key": product_key,
                        "item_id": item_id,
                        "duration_seconds": round(duration, 3),
                        "next_cycle_in_seconds": next_delay,
                    },
                )
                self._log_lifecycle(
                    "INFO",
                    "watchlist_cycle_finished",
                    {
                        "checked": 0,
                        "changed": 0,
                        "errors": 0,
                        "skipped": 0,
                        "duration_seconds": round(duration, 3),
                        "next_cycle_in_seconds": next_delay,
                    },
                )
                return {"ok": True, "checked": 0, "results": []}

            grouped: dict[str, list[dict]] = {}
            for item in items:
                grouped.setdefault(item["site"], []).append(item)

            results: list[dict[str, Any]] = []
            for current_site, site_items in grouped.items():
                concurrency = self.cfg.watchlist_max_concurrency(current_site)
                sem = asyncio.Semaphore(concurrency)
                tasks = [
                    asyncio.create_task(self._scan_one_guarded(session, sem, item))
                    for item in site_items
                ]
                site_results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in site_results:
                    if isinstance(result, Exception):
                        results.append({"ok": False, "error": f"{type(result).__name__}: {result}"})
                    else:
                        results.append(result)

            changed_count = sum(1 for result in results if (result.get("result") or {}).get("status_changed"))
            error_count = sum(
                1
                for result in results
                if not result.get("ok", True) or bool((result.get("result") or {}).get("error"))
            )
            skipped_count = sum(1 for result in results if result.get("skipped"))
            duration = time.monotonic() - started
            if self.runtime_state is not None:
                self.runtime_state.mark_cycle_finished(
                    duration_seconds=duration,
                    checked_count=len(results),
                    changed_count=changed_count,
                    error_count=error_count,
                    skipped_count=skipped_count,
                    next_cycle_in_seconds=next_delay,
                )
            self._log_lifecycle(
                "INFO",
                "watchlist_cycle_finished",
                {
                    "checked": len(results),
                    "changed": changed_count,
                    "errors": error_count,
                    "skipped": skipped_count,
                    "duration_seconds": round(duration, 3),
                    "next_cycle_in_seconds": next_delay,
                },
            )
            return {"ok": True, "checked": len(results), "results": results}
        except Exception as exc:
            duration = time.monotonic() - started
            error = f"{type(exc).__name__}: {exc}"
            if self.runtime_state is not None:
                self.runtime_state.mark_cycle_failed(
                    error=error,
                    duration_seconds=duration,
                    next_cycle_in_seconds=next_delay,
                )
            self._log_lifecycle(
                "ERROR",
                "watchlist_cycle_failed",
                {
                    "error": error,
                    "duration_seconds": round(duration, 3),
                    "next_cycle_in_seconds": next_delay,
                },
            )
            raise

    async def run_forever(self, session) -> None:
        if self.runtime_state is not None:
            self.runtime_state.mark_loop_started()
        self._log_lifecycle(
            "INFO",
            "watchlist_loop_started",
            {
                "enabled": self.cfg.watchlist_enabled(),
                "intervals": {site: self.cfg.watchlist_interval_seconds(site) for site in self.parsers},
            },
        )
        disabled_logged = False
        try:
            while True:
                if self.cfg.watchlist_enabled():
                    disabled_logged = False
                    try:
                        await self.scan_once(session)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception("[watchlist] continuous scan failed")
                        self._log_lifecycle(
                            "ERROR",
                            "watchlist_cycle_failed",
                            {"error": f"{type(exc).__name__}: {exc}"},
                        )
                elif not disabled_logged:
                    if self.runtime_state is not None:
                        self.runtime_state.mark_disabled()
                    self._log_lifecycle("INFO", "watchlist_disabled", {"enabled": False})
                    disabled_logged = True
                delay = self._cycle_delay_seconds()
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        finally:
            if self.runtime_state is not None:
                self.runtime_state.mark_loop_stopped()
            self._log_lifecycle("INFO", "watchlist_loop_stopped", {})

    async def _scan_one_guarded(self, session, sem: asyncio.Semaphore, watch_item: dict) -> dict[str, Any]:
        async with sem:
            site = watch_item["site"]
            try:
                validate_retailer_url(site, str(watch_item.get("url") or ""))
            except ValueError as exc:
                updated = self.storage.update_watchlist_check(
                    watch_item["id"],
                    status=str(watch_item.get("current_inventory_status") or "unknown"),
                    confidence=float(watch_item.get("status_confidence_score") or 0.0),
                    last_error=f"unsafe_url: {exc}",
                )
                return {
                    "id": watch_item["id"],
                    "site": site,
                    "skipped": True,
                    "reason": "unsafe_url",
                    "item": updated,
                }
            cooldown_left = max(0.0, self._cooldown_until.get(site, 0.0) - time.monotonic())
            if cooldown_left > 0:
                if self.runtime_state is not None:
                    self.runtime_state.mark_site_backoff(site, cooldown_left)
                updated = self.storage.update_watchlist_check(
                    watch_item["id"],
                    status="rate_limited_unknown",
                    confidence=0.1,
                    last_error=f"watchlist cooldown {cooldown_left:.1f}s",
                    extra={"source_endpoint": "watchlist_cooldown", "cooldown_left_seconds": round(cooldown_left, 3)},
                )
                return {"id": watch_item["id"], "site": site, "skipped": True, "cooldown_left_seconds": cooldown_left, "item": updated}

            jitter = self.cfg.watchlist_jitter_seconds(site)
            if jitter > 0:
                await asyncio.sleep(min(jitter, 0.25))

            started = time.monotonic()
            previous_status = watch_item.get("current_inventory_status") or "unknown"
            try:
                result = await self._check_item(session, watch_item)
            except Exception as exc:
                result = WatchlistCheckResult(
                    site=site,
                    product_key=watch_item["product_key"],
                    title=watch_item.get("title") or "",
                    url=watch_item.get("url") or "",
                    current_inventory_status="error",
                    status_confidence_score=0.0,
                    source_endpoint="watchlist_exception",
                    duration_seconds=time.monotonic() - started,
                    error=f"{type(exc).__name__}: {exc}",
                )

            if result.http_status == 429 and self.cfg.watchlist_backoff_on_429():
                self._register_429(site)

            status_changed = previous_status != result.current_inventory_status
            notification_queued = False
            selenium_queued = False
            skip_reason: str | None = None

            if status_changed and result.current_inventory_status in AVAILABLE_STATUSES:
                notification_queued, selenium_queued, skip_reason = await self._maybe_trigger_actions(
                    session,
                    watch_item,
                    result,
                )
            elif result.current_inventory_status not in AVAILABLE_STATUSES:
                skip_reason = "watchlist_not_high_confidence_available"

            result_extra = dict(result.extra or {})
            pdp_diagnostic = result_extra.get("pdp_diagnostic") if isinstance(result_extra, dict) else None
            updated = self.storage.update_watchlist_check(
                watch_item["id"],
                status=result.current_inventory_status,
                confidence=result.status_confidence_score,
                last_error=result.error,
                price_value=result.item.price_value if result.item else None,
                title=result.title,
                url=result.url,
                image_url=(result.item.extra or {}).get("image_url") if result.item else None,
                extra={
                    "source_endpoint": result.source_endpoint,
                    "http_status": result.http_status,
                    "duration_seconds": round(result.duration_seconds, 3),
                    "availability_text": result.availability_text,
                    "action_target_exists": result.action_target is not None,
                    "skip_reason": skip_reason,
                    "notification_queued": notification_queued,
                    "selenium_queued": selenium_queued,
                    "buyable_marker_found": bool(result_extra.get("buyable_marker_found")),
                    "alert_notify_marker_found": bool(result_extra.get("alert_notify_marker_found")),
                    "pdp_diagnostic": pdp_diagnostic,
                    "result_extra": result_extra,
                },
            )

            decision = {
                "site": site,
                "watchlist_id": watch_item["id"],
                "product_key": watch_item["product_key"],
                "article_number": watch_item.get("article_number"),
                "url": result.url,
                "source_endpoint": result.source_endpoint,
                "http_status": result.http_status,
                "duration_seconds": round(result.duration_seconds, 3),
                "previous_inventory_status": previous_status,
                "new_inventory_status": result.current_inventory_status,
                "confidence_score": result.status_confidence_score,
                "status_changed": status_changed,
                "matched_filter_ids": watch_item.get("matched_filter_ids") or [],
                "action_target_exists": result.action_target is not None,
                "notification_queued": notification_queued,
                "selenium_queued": selenium_queued,
                "skip_reason": skip_reason,
                "error": result.error,
            }
            if site == "mediamarkt" and pdp_diagnostic:
                decision["pdp_diagnostic"] = pdp_diagnostic
            watchlist_decision_logger.info(json.dumps(decision, ensure_ascii=False, separators=(",", ":")))
            watchlist_logger.info(
                "watchlist check site=%s id=%s product_key=%s status=%s previous=%s confidence=%.2f target=%s skip=%s error=%s",
                site,
                watch_item["id"],
                watch_item["product_key"],
                result.current_inventory_status,
                previous_status,
                result.status_confidence_score,
                result.action_target is not None,
                skip_reason,
                result.error,
            )
            return {"id": watch_item["id"], "site": site, "result": decision, "item": updated}

    def _mediamarkt_artifact_root(self) -> Path:
        base_dir = Path(getattr(self.cfg, "base_dir", Path.cwd()))
        return base_dir / "debug_artifacts" / "mediamarkt_watchlist"

    @staticmethod
    def _truthy_setting(value: Any) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _mediamarkt_watchlist_artifacts_enabled(self) -> bool:
        env_value = os.environ.get("MEDIAMARKT_WATCHLIST_DEBUG_ARTIFACTS")
        if env_value is not None:
            return self._truthy_setting(env_value)

        scan_settings = getattr(self.cfg, "scan_settings", {})
        watchlist_settings = scan_settings.get("watchlist", {}) if isinstance(scan_settings, dict) else {}
        if not isinstance(watchlist_settings, dict):
            return False

        sites_settings = watchlist_settings.get("sites", {})
        site_settings = sites_settings.get("mediamarkt", {}) if isinstance(sites_settings, dict) else {}
        if isinstance(site_settings, dict) and "debug_artifacts_enabled" in site_settings:
            return self._truthy_setting(site_settings.get("debug_artifacts_enabled"))
        if "mediamarkt_debug_artifacts_enabled" in watchlist_settings:
            return self._truthy_setting(watchlist_settings.get("mediamarkt_debug_artifacts_enabled"))
        return False

    def _mediamarkt_watchlist_artifact_limit(self) -> int:
        raw = os.environ.get("MEDIAMARKT_WATCHLIST_DEBUG_ARTIFACT_LIMIT")
        if raw is None:
            scan_settings = getattr(self.cfg, "scan_settings", {})
            watchlist_settings = scan_settings.get("watchlist", {}) if isinstance(scan_settings, dict) else {}
            sites_settings = watchlist_settings.get("sites", {}) if isinstance(watchlist_settings, dict) else {}
            site_settings = (
                sites_settings.get("mediamarkt", {})
                if isinstance(sites_settings, dict)
                else {}
            )
            raw = site_settings.get("debug_artifact_limit") if isinstance(site_settings, dict) else None
        try:
            return max(0, int(str(raw).strip())) if raw is not None and raw != "" else 20
        except Exception:
            return 20

    @staticmethod
    def _headers_to_dict(headers: Any) -> dict[str, str]:
        if not headers:
            return {}
        try:
            return {str(key): str(value) for key, value in dict(headers).items()}
        except Exception:
            return {}

    def _save_mediamarkt_watchlist_artifacts(
        self,
        *,
        watch_item: dict,
        reason: str,
        html: str | None,
        response_headers: Any = None,
        final_url: str | None = None,
        diagnostic: dict[str, Any] | None = None,
        http_status: int | None = None,
    ) -> str | None:
        if not self._mediamarkt_watchlist_artifacts_enabled():
            return None

        artifact_limit = self._mediamarkt_watchlist_artifact_limit()
        if self._mediamarkt_artifacts_saved >= artifact_limit:
            if not self._mediamarkt_artifact_limit_logged:
                watchlist_logger.warning(
                    "[watchlist] MediaMarkt diagnostic artifact limit reached limit=%s",
                    artifact_limit,
                )
                self._mediamarkt_artifact_limit_logged = True
            return None

        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            product_key = str(watch_item.get("product_key") or watch_item.get("article_number") or "unknown")
            safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", product_key)[:80] or "unknown"
            root = self._mediamarkt_artifact_root()
            artifact_dir = root / f"{timestamp}_{watch_item.get('id', 'unknown')}_{safe_key}_{reason}"
            artifact_dir.mkdir(parents=True, exist_ok=True)

            if html is not None:
                (artifact_dir / "response.html").write_text(html, encoding="utf-8", errors="replace")

            metadata = {
                "timestamp": timestamp,
                "watchlist_id": watch_item.get("id"),
                "product_key": watch_item.get("product_key"),
                "article_number": watch_item.get("article_number"),
                "reason": reason,
                "final_url": final_url or watch_item.get("url"),
                "http_status": http_status,
                "response_headers": self._headers_to_dict(response_headers),
                "diagnostic": diagnostic or {},
            }
            (artifact_dir / "diagnostic.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._mediamarkt_artifacts_saved += 1
            return str(artifact_dir)
        except Exception:
            logger.exception("[watchlist] failed to save MediaMarkt diagnostic artifacts")
            return None

    @staticmethod
    def _mediamarkt_needs_artifact(
        *,
        status: str,
        diagnostic: dict[str, Any] | None,
        action_target_exists: bool,
        error: str | None = None,
    ) -> str | None:
        diag = diagnostic or {}
        buyable_marker_found = bool(
            diag.get("add_to_cart_button_found")
            or diag.get("delivery_available_marker")
            or diag.get("online_status_available_marker")
        )
        if "html_rate_limited" in str(error or ""):
            return "html_rate_limited"
        if status == "parse_unknown":
            return "parse_unknown"
        if diag.get("conflicting_signals"):
            return "conflicting_signals"
        if buyable_marker_found and not action_target_exists:
            return "available_markers_without_action_target"
        return None

    def _register_429(self, site: str) -> None:
        interval = self.cfg.watchlist_interval_seconds(site)
        current_left = max(0.0, self._cooldown_until.get(site, 0.0) - time.monotonic())
        next_seconds = max(interval, current_left * self.cfg.watchlist_backoff_multiplier() or interval)
        next_seconds = min(next_seconds, self.cfg.watchlist_max_backoff_seconds())
        self._cooldown_until[site] = time.monotonic() + next_seconds
        if self.runtime_state is not None:
            self.runtime_state.mark_site_backoff(site, next_seconds)
        self._log_lifecycle(
            "WARNING",
            "watchlist_site_backoff",
            {"site": site, "backoff_seconds": round(next_seconds, 3)},
        )

    async def _check_item(self, session, watch_item: dict) -> WatchlistCheckResult:
        site = watch_item["site"]
        if site == "mediamarkt":
            return await self._check_mediamarkt(session, watch_item)
        if site == "dreamland":
            return await self._check_dreamland(session, watch_item)
        if site == "bol":
            return await self._check_bol(session, watch_item)
        if site == "pocketgames":
            return await self._check_pocketgames(session, watch_item)
        raise ValueError(f"unsupported watchlist site: {site}")

    async def _check_mediamarkt(self, session, watch_item: dict) -> WatchlistCheckResult:
        parser: MediaMarktParser = self.parsers["mediamarkt"]
        url = watch_item.get("url") or ""
        started = time.monotonic()
        try:
            html = await parser._fetch_html(session, self.cfg, url)
        except MediaMarktParserDeny as exc:
            status = "rate_limited_unknown"
            artifact_dir = self._save_mediamarkt_watchlist_artifacts(
                watch_item=watch_item,
                reason="html_rate_limited",
                html=None,
                response_headers=getattr(exc, "headers", None),
                final_url=url,
                diagnostic={},
                http_status=getattr(exc, "status", None),
            )
            return WatchlistCheckResult(
                site="mediamarkt",
                product_key=watch_item["product_key"],
                title=watch_item.get("title") or "",
                url=url,
                current_inventory_status=status,
                status_confidence_score=0.0,
                source_endpoint="pdp_html",
                http_status=getattr(exc, "status", None),
                duration_seconds=time.monotonic() - started,
                error=getattr(exc, "message", "") or str(exc),
                extra={"debug_artifact_dir": artifact_dir, "rate_limited": True},
            )

        item = parser.parse_pdp_html(html, url)
        if item is None:
            status = "parse_unknown"
            confidence = 0.2
            title = watch_item.get("title") or ""
            result_extra: dict[str, Any] = {"pdp_diagnostic": parser.diagnose_pdp_html(html, url)}
            action_target = None
        else:
            status = str(item.extra.get("availability_status") or "parse_unknown")
            confidence = float(item.extra.get("status_confidence_score") or 0.0)
            title = item.title
            result_extra = dict(item.extra or {})
            action_target = item.target

        diagnostic = result_extra.get("pdp_diagnostic")
        artifact_reason = self._mediamarkt_needs_artifact(
            status=status,
            diagnostic=diagnostic if isinstance(diagnostic, dict) else None,
            action_target_exists=action_target is not None,
        )
        if artifact_reason:
            artifact_dir = self._save_mediamarkt_watchlist_artifacts(
                watch_item=watch_item,
                reason=artifact_reason,
                html=html,
                response_headers=None,
                final_url=url,
                diagnostic=diagnostic if isinstance(diagnostic, dict) else {},
                http_status=200,
            )
            if artifact_dir:
                result_extra["debug_artifact_dir"] = artifact_dir

        return WatchlistCheckResult(
            site="mediamarkt",
            product_key=watch_item["product_key"],
            title=title,
            url=url,
            current_inventory_status=status,
            status_confidence_score=confidence,
            is_available=bool(item.is_available) if item else False,
            availability_text=item.availability_text if item else None,
            source_endpoint="pdp_html",
            http_status=200,
            duration_seconds=time.monotonic() - started,
            item=item,
            action_target=action_target,
            extra=result_extra,
        )

    async def _check_dreamland(self, session, watch_item: dict) -> WatchlistCheckResult:
        parser: DreamLandParser = self.parsers["dreamland"]
        url = watch_item.get("url") or ""
        started = time.monotonic()
        try:
            html = await parser.fetch_product(session, self.cfg, url)
        except DreamLandParserDeny as exc:
            return WatchlistCheckResult(
                site="dreamland",
                product_key=watch_item["product_key"],
                title=watch_item.get("title") or "",
                url=url,
                current_inventory_status="rate_limited_unknown",
                status_confidence_score=0.0,
                source_endpoint="pdp_html",
                http_status=getattr(exc, "status", None),
                duration_seconds=time.monotonic() - started,
                error=getattr(exc, "message", "") or str(exc),
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                return WatchlistCheckResult(
                    site="dreamland",
                    product_key=watch_item["product_key"],
                    title=watch_item.get("title") or "",
                    url=url,
                    current_inventory_status="not_found_currently",
                    status_confidence_score=0.7,
                    source_endpoint="pdp_html",
                    http_status=404,
                    duration_seconds=time.monotonic() - started,
                    error="pdp_404",
                )
            raise

        soup = BeautifulSoup(html, "html.parser")
        title = parser._parse_pdp_title(soup, watch_item.get("title") or "")
        availability = parser._parse_pdp_availability(soup)
        price, _price_source = parser._parse_pdp_price(html, soup, watch_item.get("price_value"))
        external_id = parser._extract_product_code(url, html)
        status = "in_stock" if availability.purchasable else availability.status or "out_of_stock"
        target = None
        if availability.purchasable:
            target = parser._build_target(external_id, title, url, 0, watch_item.get("image_url"), _price_source, "watchlist_pdp")
        item = ParsedItem(
            site="dreamland",
            external_id=external_id,
            title=title,
            title_norm=normalize_text(title),
            url=url,
            price_value=price,
            availability_text=availability.reason,
            is_available=availability.purchasable,
            seller="dreamland",
            extra={
                "availability_status": status,
                "availability_source": "watchlist_pdp",
                "status_confidence_score": 1.0 if availability.purchasable else 0.75,
                "purchasable": availability.purchasable,
                "positive_signals": list(availability.positive_signals),
                "negative_signals": list(availability.negative_signals),
            },
            target=target,
        )
        return WatchlistCheckResult(
            site="dreamland",
            product_key=watch_item["product_key"],
            title=title,
            url=url,
            current_inventory_status=status,
            status_confidence_score=float(item.extra["status_confidence_score"]),
            is_available=item.is_available,
            availability_text=item.availability_text,
            source_endpoint="pdp_html",
            http_status=200,
            duration_seconds=time.monotonic() - started,
            item=item,
            action_target=target,
            extra=dict(item.extra),
        )

    async def _check_bol(self, session, watch_item: dict) -> WatchlistCheckResult:
        parser: BolParser = self.parsers["bol"]
        url = watch_item.get("url") or ""
        started = time.monotonic()
        html = await parser.fetch_product(session, self.cfg, url)
        soup = BeautifulSoup(html, "html.parser")
        title = clean_text((soup.select_one("h1") or soup).get_text(" ", strip=True))[:180] or watch_item.get("title") or ""
        product_id = parser._extract_product_id_from_url(url)
        offer_uid = parser._extract_offer_uid(html)
        add_url = parser._build_add_to_cart_url(product_id, offer_uid) if product_id and offer_uid else None
        page_text = clean_text(soup.get_text(" ", strip=True)).lower()
        available = bool(add_url or "op voorraad" in page_text or "huidige voorraad bijna op" in page_text)
        status = "offer_available" if available else ("out_of_stock" if "niet leverbaar" in page_text else "parse_unknown")
        target = None
        if available:
            target = ActionTarget(
                site="bol",
                external_id=watch_item["product_key"],
                title=title,
                product_url=url,
                add_to_cart=AddToCartTarget(
                    type="direct_url",
                    quantity=1,
                    add_to_cart_url=add_url,
                    product_id=product_id,
                    offer_uid=offer_uid,
                    product_url=url,
                ),
                checkout=CheckoutTarget(type="url", checkout_url=self.cfg.bol_buy_now_url),
                meta={"product_id": product_id, "offer_uid": offer_uid, "source": "watchlist_pdp"},
            )
        item = ParsedItem(
            site="bol",
            external_id=watch_item["product_key"],
            title=title,
            title_norm=normalize_text(title),
            url=url,
            price_value=parse_price_eur(page_text),
            availability_text=status,
            is_available=available,
            seller="bol",
            extra={"product_id": product_id, "offer_uid": offer_uid, "availability_status": status, "status_confidence_score": 1.0 if available else 0.5},
            target=target,
        )
        return WatchlistCheckResult(
            site="bol",
            product_key=watch_item["product_key"],
            title=title,
            url=url,
            current_inventory_status=status,
            status_confidence_score=float(item.extra["status_confidence_score"]),
            is_available=available,
            availability_text=status,
            source_endpoint="pdp_html",
            http_status=200,
            duration_seconds=time.monotonic() - started,
            item=item,
            action_target=target,
            extra=dict(item.extra),
        )

    async def _check_pocketgames(self, session, watch_item: dict) -> WatchlistCheckResult:
        parser: PocketGamesParser = self.parsers["pocketgames"]
        handle = watch_item.get("handle") or watch_item.get("product_key") or ""
        if not handle and watch_item.get("url"):
            handle = watch_item["url"].rstrip("/").split("/")[-1]
        url = f"{parser.base_url}/products/{handle}"
        json_url = f"{url}.json"
        started = time.monotonic()
        try:
            async with session.get(json_url, timeout=self.cfg.watchlist_request_timeout_seconds("pocketgames")) as response:
                if response.status == 404:
                    return WatchlistCheckResult(
                        site="pocketgames",
                        product_key=handle,
                        title=watch_item.get("title") or "",
                        url=url,
                        current_inventory_status="not_found_currently",
                        status_confidence_score=0.7,
                        source_endpoint="shopify_product_json",
                        http_status=404,
                        duration_seconds=time.monotonic() - started,
                        error="product_json_404",
                    )
                response.raise_for_status()
                payload = await response.json()
        except aiohttp.ClientResponseError as exc:
            status = "http_429_throttled" if exc.status == 429 else "error"
            return WatchlistCheckResult(
                site="pocketgames",
                product_key=handle,
                title=watch_item.get("title") or "",
                url=url,
                current_inventory_status=status,
                status_confidence_score=0.0,
                source_endpoint="shopify_product_json",
                http_status=exc.status,
                duration_seconds=time.monotonic() - started,
                error=f"http_{exc.status}",
            )

        product = payload.get("product") or {}
        title = clean_text(product.get("title") or watch_item.get("title") or "")
        variants = product.get("variants") or []
        available_variant = next((variant for variant in variants if variant.get("available")), None)
        available = available_variant is not None
        variant_id = available_variant.get("id") if available_variant else None
        price = None
        try:
            if variants:
                price = float(variants[0].get("price"))
        except Exception:
            price = None
        target = None
        if available:
            target = ActionTarget(
                site="pocketgames",
                external_id=handle,
                title=title,
                product_url=url,
                add_to_cart=AddToCartTarget(
                    type="shopify_variant",
                    quantity=1,
                    variant_id=variant_id,
                    product_id=product.get("id"),
                    cart_add_url=f"{parser.base_url}/cart/add",
                    cart_url=f"{parser.base_url}/cart",
                    product_url=url,
                    section_id=parser.DEFAULT_SECTION_ID,
                    sections_url=parser.DEFAULT_SECTIONS_URL,
                ),
                checkout=CheckoutTarget(type="shopify_cart", cart_url=f"{parser.base_url}/cart", checkout_url=f"{parser.base_url}/checkout"),
                meta={"product_id": product.get("id"), "variant_id": variant_id, "handle": handle, "source": "watchlist_json"},
            )
        status = "variant_available" if available else "out_of_stock"
        item = ParsedItem(
            site="pocketgames",
            external_id=handle,
            title=title,
            title_norm=normalize_text(title),
            url=url,
            price_value=price,
            availability_text=status,
            is_available=available,
            seller="pocketgames",
            extra={"product_id": product.get("id"), "variant_id": variant_id, "handle": handle, "availability_status": status, "status_confidence_score": 1.0 if available else 0.8},
            target=target,
        )
        return WatchlistCheckResult(
            site="pocketgames",
            product_key=handle,
            title=title,
            url=url,
            current_inventory_status=status,
            status_confidence_score=float(item.extra["status_confidence_score"]),
            is_available=available,
            availability_text=status,
            source_endpoint="shopify_product_json",
            http_status=200,
            duration_seconds=time.monotonic() - started,
            item=item,
            action_target=target,
            extra=dict(item.extra),
        )

    async def _maybe_trigger_actions(
        self,
        session,
        watch_item: dict,
        result: WatchlistCheckResult,
    ) -> tuple[bool, bool, str | None]:
        if result.status_confidence_score < 0.9 or result.action_target is None or result.item is None:
            return False, False, "watchlist_available_low_confidence_or_missing_target"

        filters = self.storage.load_filters()
        matched = [rule for rule in filters if match_precheck(result.item, rule) and match(result.item, rule)]
        if not matched and not bool(watch_item.get("pinned")):
            self.storage.update_watchlist_item(watch_item["id"], {"orphaned": True})
            return False, False, "watchlist_orphaned_no_enabled_filter"

        notification_queued = False
        selenium_queued = False
        if self.notifier is not None and self.notifier.is_enabled() and self.cfg.enable_notifications:
            await self.notifier.send(
                session,
                f"[watchlist][{result.site}] available\n{result.title}\n{result.url}\nstatus={result.current_inventory_status}",
                metadata={
                    "source": "watchlist",
                    "site": result.site,
                    "product_key": result.product_key,
                    "watchlist_id": watch_item["id"],
                },
            )
            notification_queued = True

        if self.selenium_dispatcher is None or self.cfg.action_mode != "selenium":
            return notification_queued, False, "ready_for_action"

        if self.storage.was_already_queued(result.site, result.product_key, within_minutes=10):
            return notification_queued, False, "watchlist_duplicate_recent_queue"

        job = SeleniumJob(
            site=result.site,
            case="add_to_cart_and_checkout",
            target=result.action_target,
            action_id=f"watchlist-{result.site}-{uuid.uuid4().hex[:8]}",
            created_at=utc_now_iso(),
            metadata={
                "event": "watchlist_action_created",
                "watchlist_id": watch_item["id"],
                "product_key": result.product_key,
                "availability_status": result.current_inventory_status,
                "source": "watchlist",
                "matched_filter_ids": [rule.id for rule in matched],
            },
        )
        submit_result = self.selenium_dispatcher.submit(job)
        selenium_queued = submit_result.status == "queued"
        self.storage.insert_action_log(
            result.site,
            result.product_key,
            "selenium_queue",
            "add_to_cart_and_checkout",
            submit_result.status,
            json.dumps(
                {
                    "event": "watchlist_action_created",
                    "watchlist_id": watch_item["id"],
                    "title": result.title,
                    "url": result.url,
                    "action_id": job.action_id,
                    "job_key": submit_result.job_key,
                    "availability_status": result.current_inventory_status,
                    "source": "watchlist",
                    "purchase_key": purchase_key_for_target(result.action_target),
                },
                ensure_ascii=False,
            ),
        )
        return notification_queued, selenium_queued, "ready_for_action" if selenium_queued else submit_result.status
