from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import replace

import aiohttp

from pokemon_parser.config import AppConfig
from pokemon_parser.engine.antiban import AntiBanManager
from pokemon_parser.engine.runtime_state import RuntimeStateStore, SiteRunResult
from pokemon_parser.engine.scheduler import Scheduler
from pokemon_parser.engine.selenium_dispatcher import SeleniumDispatcher
from pokemon_parser.engine.startup import ZERO_ENABLED_FILTERS_WARNING
from pokemon_parser.engine.watchlist import WatchlistTracker
from pokemon_parser.filters.engine import explain, match, match_precheck
from pokemon_parser.models import ParsedItem, SeleniumJob
from pokemon_parser.notifications.telegram import TelegramNotifier
from pokemon_parser.parsers import build_enabled_parser_registry
from pokemon_parser.parsers.dreamland import DreamLandParserDeny
from pokemon_parser.parsers.mediamarkt import MediaMarktParserDeny
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.utils.proxy import ProxyAwareSession, reset_http_diagnostic_context, set_http_diagnostic_context
from pokemon_parser.utils.time import utc_now_iso
from pokemon_parser.workers.purchase_safety import (
    BLOCKING_PURCHASE_STATUSES,
    PURCHASE_STATUS_QUEUED,
    duplicate_skip_status,
    purchase_key_for_target,
)

logger = logging.getLogger(__name__)
scan_decision_logger = logging.getLogger("pokemon_parser.scan_decisions")
NOTIFICATION_TRANSITIONS = {
    "new_item",
    "price_changed",
    "restock",
    "seller_changed",
    "returned_to_listing",
}
PURCHASE_TRANSITIONS = {"new_item", "restock", "returned_to_listing"}


class Pipeline:
    def __init__(
        self,
        cfg: AppConfig,
        storage: SqliteStorage,
        notifier: TelegramNotifier,
        selenium_dispatcher: SeleniumDispatcher | None = None,
        antiban: AntiBanManager | None = None,
        runtime_state: RuntimeStateStore | None = None,
        watchlist_runtime_state=None,
    ) -> None:
        self.cfg = cfg
        self.storage = storage
        self.notifier = notifier
        self.selenium_dispatcher = selenium_dispatcher
        self.antiban = antiban
        self.runtime_state = runtime_state
        self.watchlist_runtime_state = watchlist_runtime_state
        self.parsers = build_enabled_parser_registry(cfg)
        self.scan_semaphore = asyncio.Semaphore(max(1, int(cfg.parser_concurrency)))

        if self.runtime_state is not None:
            self.runtime_state.set_enabled_map(cfg.parser_enabled_map())

    def _log_runtime(
        self,
        level: str,
        category: str,
        message: str,
        *,
        site: str | None = None,
        details: dict | None = None,
    ) -> None:
        prefix = f"[{category}]"
        if site:
            prefix += f"[{site}]"

        log_method = getattr(logger, level.lower(), logger.info)
        log_method("%s %s", prefix, message)

        try:
            self.storage.insert_runtime_log(
                level=level,
                category=category,
                message=message,
                site=site,
                details=details,
            )
        except Exception:
            logger.exception("[runtime] failed to persist runtime log category=%s site=%s", category, site)

        should_alert = (
            category == "error"
            and level.upper() in {"ERROR", "CRITICAL"}
            and self.cfg.enable_notifications
            and self.cfg.enable_error_alerts
            and self.notifier.is_enabled()
        )
        if should_alert:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._send_error_alert(message=message, site=site))
            except RuntimeError:
                pass

    async def _send_error_alert(self, *, message: str, site: str | None = None) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as raw_session:
            session = ProxyAwareSession(raw_session, self.cfg)
            prefix = f"[error]{f'[{site}]' if site else ''}"
            await self.notifier.send(session, f"{prefix} {message}")

    async def _send_pocketgames_safety_alert(self, session, *, title: str, status: str, reason: str) -> None:
        if not (
            self.cfg.enable_notifications
            and self.cfg.worker_telegram_trace_enabled
            and self.notifier.is_enabled()
        ):
            return
        try:
            await self.notifier.send(
                session,
                "[pocketgames] purchase safety\n"
                f"Product: {title}\n"
                f"Status: {status}\n"
                f"Reason: {reason}",
                metadata={
                    "site": "pocketgames",
                    "title": title,
                    "status": status,
                    "reason": reason,
                },
            )
        except Exception as exc:
            self._log_runtime(
                "WARNING",
                "worker_trace",
                f"pocketgames safety telegram failed error={exc}",
                site="pocketgames",
            )

    def _parser_cooldown_left(self, site: str) -> float:
        if self.antiban is None or site not in self.antiban.state:
            return 0.0
        return max(0.0, self.antiban.state[site].parser.cooldown_until - time.monotonic())

    @staticmethod
    def _action_block_reason(item: ParsedItem) -> tuple[str, dict] | None:
        details = {
            "availability_text": item.availability_text,
            "is_available": item.is_available,
            "purchasable": item.extra.get("purchasable", item.is_available),
            "availability_status": item.extra.get("availability_status"),
            "availability_reason": item.extra.get("availability_reason") or item.availability_text,
            "negative_signals": item.extra.get("negative_signals"),
            "positive_signals": item.extra.get("positive_signals"),
            "target_present": item.target is not None,
        }

        if item.site == "dreamland":
            if not item.is_available or not bool(item.extra.get("purchasable", item.is_available)):
                return "dreamland_unavailable_signal", details
            if item.target is None:
                return "dreamland_missing_action_target", details
            return None

        if not item.is_available:
            return f"{item.site}_unavailable", details

        if item.target is None:
            return f"{item.site}_missing_action_target", details

        return None

    @staticmethod
    def _new_scan_id(site: str) -> str:
        return f"{site}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _page_count(items: list[ParsedItem]) -> int | None:
        pages: set[int] = set()
        for item in items:
            try:
                page = item.extra.get("page") if item.extra else None
                if page is not None:
                    pages.add(int(page))
            except Exception:
                continue
        return len(pages) if pages else None

    @staticmethod
    def _soft_rate_limit_item_count(site: str, items: list[ParsedItem]) -> int:
        if site != "mediamarkt":
            return 0
        count = 0
        for item in items:
            extra = item.extra or {}
            if extra.get("rate_limited") or extra.get("availability_status") == "rate_limited_unknown":
                count += 1
        return count

    @staticmethod
    def _parser_scan_metrics(parser) -> dict:
        metrics = getattr(parser, "last_scan_metrics", None)
        return dict(metrics) if isinstance(metrics, dict) else {}

    def _log_parser_endpoint_metrics(self, *, site: str, scan_id: str, metrics: dict) -> None:
        if not metrics:
            return

        routing_mode = metrics.get("discovery_routing_mode") or "normal"
        endpoint_status = metrics.get("graphql_endpoint_status") or "active"
        status = metrics.get("scan_status") or "success"
        details = {
            "scan_id": scan_id,
            "endpoint_status": endpoint_status,
            "graphql_circuit_open": bool(metrics.get("graphql_circuit_open", False)),
            "graphql_backoff_until": metrics.get("graphql_backoff_until"),
            "discovery_routing_mode": routing_mode,
            "failure_severity": metrics.get("failure_severity"),
            "source_mix": metrics.get("source_mix") or {},
            "items_fetched": metrics.get("items_fetched", 0),
            "global_cooldown_applied": bool(metrics.get("global_cooldown_applied", False)),
            "isolated_backoff_applied": bool(metrics.get("isolated_backoff_applied", False)),
            "graphql_soft_denies": metrics.get("graphql_soft_denies", 0),
            "graphql_partial_successes": metrics.get("graphql_partial_successes", 0),
            "graphql_valid_empty_pages": metrics.get("graphql_valid_empty_pages", 0),
            "graphql_transient_failures": metrics.get("graphql_transient_failures", 0),
            "graphql_strong_denies": metrics.get("graphql_strong_denies", 0),
            "graphql_parse_failures": metrics.get("graphql_parse_failures", 0),
            "graphql_outcome_counts": metrics.get("graphql_outcome_counts") or {},
            "graphql_reason_counts": metrics.get("graphql_reason_counts") or {},
            "graphql_access_state": metrics.get("graphql_access_state"),
            "graphql_last_reason_code": metrics.get("graphql_last_reason_code"),
            "html_fallback_pages": metrics.get("html_fallback_pages", 0),
            "html_fallback_pages_with_products": metrics.get("html_fallback_pages_with_products", 0),
            "fallback_routing_only_pages": metrics.get("fallback_routing_only_pages", 0),
        }
        self._log_runtime(
            "INFO",
            "scan",
            (
                f"endpoint_status scan_id={scan_id} endpoint={endpoint_status} "
                f"routing={routing_mode} backoff_until={metrics.get('graphql_backoff_until') or '-'}"
            ),
            site=site,
            details=details,
        )

        if status in {"partial_success", "recovered_via_fallback"}:
            self._log_runtime(
                "WARNING" if status == "partial_success" else "INFO",
                "scan",
                (
                    f"{status} scan_id={scan_id} source_mix={metrics.get('source_mix') or {}} "
                    f"global_cooldown=false isolated_backoff={str(bool(metrics.get('isolated_backoff_applied'))).lower()} "
                    f"routing={routing_mode}"
                ),
                site=site,
                details=details,
            )

        for event in metrics.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_name = str(event.get("event") or "")
            if not event_name:
                continue
            self._log_runtime(
                "WARNING" if event_name == "graphql_circuit_open" else "INFO",
                "scan",
                (
                    f"{event_name} scan_id={scan_id} "
                    f"routing={event.get('discovery_routing_mode') or routing_mode} "
                    f"backoff_until={event.get('backoff_until') or event.get('graphql_backoff_until') or '-'} "
                    f"fallback_routing_only={str(event_name == 'graphql_circuit_active').lower()}"
                ),
                site=site,
                details={"scan_id": scan_id, **event},
            )

    def endpoint_status_snapshot(self) -> dict[str, dict]:
        snapshots: dict[str, dict] = {}
        for site, parser in self.parsers.items():
            if hasattr(parser, "runtime_endpoint_snapshot"):
                try:
                    snapshots[site] = parser.runtime_endpoint_snapshot()
                except Exception:
                    logger.exception("[runtime] failed to read endpoint snapshot site=%s", site)
        return snapshots

    def _log_scan_decision(
        self,
        *,
        scan_id: str,
        item: ParsedItem,
        previous_state: dict | None,
        event_types: set[str],
        matched_rules: list,
        skip_reason: str,
        notification_queued: bool,
        selenium_job_queued: bool,
        action_id: str | None = None,
    ) -> None:
        payload = {
            "scan_id": scan_id,
            "site": item.site,
            "product_id": item.external_id,
            "title": item.title,
            "price": item.price_value,
            "available": item.is_available,
            "previous_available": (
                previous_state.get("is_available") if previous_state is not None else None
            ),
            "is_new": "new_item" in event_types,
            "restock": "restock" in event_types,
            "matched_filter_ids": [rule.id for rule in matched_rules],
            "matched_filter_names": [rule.name for rule in matched_rules],
            "skip_reason": skip_reason,
            "action_target_exists": item.target is not None,
            "notification_queued": notification_queued,
            "selenium_job_queued": selenium_job_queued,
            "action_id": action_id,
            "source": item.extra.get("source") if item.extra else None,
            "availability_status": item.extra.get("availability_status") if item.extra else None,
            "availability_confidence": item.extra.get("availability_confidence") if item.extra else None,
            "availability_reason": item.extra.get("availability_reason") if item.extra else None,
            "purchasable": item.extra.get("purchasable", item.is_available) if item.extra else item.is_available,
            "rate_limited": bool(item.extra.get("rate_limited")) if item.extra else False,
        }
        scan_decision_logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def run_site(self, session: aiohttp.ClientSession, parser) -> SiteRunResult:
        site = parser.site
        scan_id = self._new_scan_id(site)
        scan_started = time.monotonic()
        self._log_runtime(
            "INFO",
            "scan",
            f"starting parser scan scan_id={scan_id}",
            site=site,
            details={"scan_id": scan_id, "parser": type(parser).__name__},
        )

        if self.antiban is not None:
            allowed, reason = self.antiban.allow(site, "parser")
            if not allowed:
                self._log_runtime(
                    "INFO",
                    "scheduler",
                    f"scan skipped scan_id={scan_id} reason={reason}",
                    site=site,
                    details={"scan_id": scan_id, "reason": reason},
                )
                return SiteRunResult(site=site, status="skipped", message=f"skipped_{reason}")

            self.antiban.mark_op(site, "parser")
            if reason == "half_open_probe":
                self.antiban.use_half_open_probe(site, "parser")

        try:
            context_tokens = set_http_diagnostic_context(scan_id=scan_id, site=site)
            try:
                items = await parser.fetch(session, self.cfg)
            finally:
                reset_http_diagnostic_context(context_tokens)
            parser_metrics = self._parser_scan_metrics(parser)
            self._log_parser_endpoint_metrics(site=site, scan_id=scan_id, metrics=parser_metrics)
            if self.antiban is not None:
                self.antiban.report_success(site, "parser")
            scan_status = str(parser_metrics.get("scan_status") or "success")
            if scan_status not in {"success", "partial_success", "recovered_via_fallback"}:
                scan_status = "success"
        except (MediaMarktParserDeny, DreamLandParserDeny) as exc:
            failure_metrics = self._parser_scan_metrics(parser)
            self._log_parser_endpoint_metrics(site=site, scan_id=scan_id, metrics=failure_metrics)
            deny_kind = getattr(exc, "message", None) or f"http_{getattr(exc, 'status', 'deny')}"
            retry_after = None

            try:
                header_value = exc.headers.get("Retry-After") if exc.headers else None
                if header_value and str(header_value).isdigit():
                    retry_after = float(header_value)
            except Exception:
                retry_after = None

            cooldown = 0.0
            if self.antiban is not None:
                cooldown = self.antiban.report_deny(
                    site,
                    "parser",
                    deny_kind,
                    retry_after_seconds=retry_after,
                )

            self._log_runtime(
                "WARNING",
                "error",
                f"parser deny scan_id={scan_id} status={getattr(exc, 'status', None)} kind={deny_kind} cooldown={cooldown:.1f}s",
                site=site,
                details={
                    "scan_id": scan_id,
                    "status": getattr(exc, "status", None),
                    "deny_kind": deny_kind,
                    "cooldown_seconds": cooldown,
                    "failure_severity": "critical_channel_failure",
                    "global_cooldown_applied": cooldown > 0,
                    "isolated_backoff_applied": bool(failure_metrics.get("isolated_backoff_applied", False)),
                    "duration_seconds": round(time.monotonic() - scan_started, 3),
                },
            )
            return SiteRunResult(site=site, status="error", message=f"parser_deny_{deny_kind}")

        except aiohttp.ClientResponseError as exc:
            retry_after = None
            try:
                header_value = exc.headers.get("Retry-After") if exc.headers else None
                if header_value and str(header_value).isdigit():
                    retry_after = float(header_value)
            except Exception:
                retry_after = None

            deny_kind = f"http_{exc.status}"
            cooldown = 0.0
            if self.antiban is not None:
                cooldown = self.antiban.report_deny(
                    site,
                    "parser",
                    deny_kind,
                    retry_after_seconds=retry_after,
                )

            self._log_runtime(
                "ERROR",
                "error",
                f"parser http failure scan_id={scan_id} status={exc.status} cooldown={cooldown:.1f}s error={exc}",
                site=site,
                details={
                    "scan_id": scan_id,
                    "status": exc.status,
                    "deny_kind": deny_kind,
                    "cooldown_seconds": cooldown,
                    "duration_seconds": round(time.monotonic() - scan_started, 3),
                },
            )
            return SiteRunResult(site=site, status="error", message=f"parser_http_{exc.status}")

        except asyncio.TimeoutError as exc:
            deny_kind = "timeout"
            cooldown = 0.0
            if self.antiban is not None:
                cooldown = self.antiban.report_deny(
                    site,
                    "parser",
                    deny_kind,
                    retry_after_seconds=None,
                )

            self._log_runtime(
                "WARNING",
                "error",
                f"parser timeout scan_id={scan_id} cooldown={cooldown:.1f}s error={type(exc).__name__}: {exc}",
                site=site,
                details={
                    "scan_id": scan_id,
                    "deny_kind": deny_kind,
                    "cooldown_seconds": cooldown,
                    "parser": type(parser).__name__,
                    "duration_seconds": round(time.monotonic() - scan_started, 3),
                },
            )
            return SiteRunResult(site=site, status="error", message="parser_timeout")

        except aiohttp.ClientError as exc:
            deny_kind = type(exc).__name__
            cooldown = 0.0
            if self.antiban is not None:
                cooldown = self.antiban.report_deny(
                    site,
                    "parser",
                    deny_kind,
                    retry_after_seconds=None,
                )

            self._log_runtime(
                "WARNING",
                "error",
                f"parser client failure scan_id={scan_id} cooldown={cooldown:.1f}s error={type(exc).__name__}: {exc}",
                site=site,
                details={
                    "scan_id": scan_id,
                    "deny_kind": deny_kind,
                    "cooldown_seconds": cooldown,
                    "parser": type(parser).__name__,
                    "duration_seconds": round(time.monotonic() - scan_started, 3),
                },
            )
            return SiteRunResult(site=site, status="error", message=f"parser_client_{type(exc).__name__}")

        except Exception as exc:
            deny_kind = type(exc).__name__
            cooldown = 0.0
            if self.antiban is not None:
                cooldown = self.antiban.report_deny(
                    site,
                    "parser",
                    deny_kind,
                    retry_after_seconds=None,
                )

            logger.exception("[scan][%s] parser crashed scan_id=%s", site, scan_id)
            self._log_runtime(
                "ERROR",
                "error",
                f"parser crashed scan_id={scan_id} cooldown={cooldown:.1f}s error={type(exc).__name__}: {exc}",
                site=site,
                details={
                    "scan_id": scan_id,
                    "deny_kind": deny_kind,
                    "cooldown_seconds": cooldown,
                    "duration_seconds": round(time.monotonic() - scan_started, 3),
                },
            )
            return SiteRunResult(site=site, status="error", message=f"{type(exc).__name__}: {exc}")

        self._log_runtime(
            "INFO",
            "scan",
            f"parser finished scan_id={scan_id} status={scan_status} items={len(items)} duration={time.monotonic() - scan_started:.2f}s",
            site=site,
            details={
                "scan_id": scan_id,
                "scan_status": scan_status,
                "items_found": len(items),
                "available_count": sum(1 for item in items if item.is_available),
                "unavailable_count": sum(1 for item in items if not item.is_available),
                "target_count": sum(1 for item in items if item.target is not None),
                "page_count": self._page_count(items),
                "source_mix": parser_metrics.get("source_mix") if parser_metrics else None,
                "global_cooldown_applied": False,
                "isolated_backoff_applied": bool(parser_metrics.get("isolated_backoff_applied", False)),
                "discovery_routing_mode": parser_metrics.get("discovery_routing_mode") if parser_metrics else None,
                "duration_seconds": round(time.monotonic() - scan_started, 3),
            },
        )

        try:
            all_filters = self.storage.list_filters_all()
            filters = [rule for rule in all_filters if rule.enabled]
            decision_metrics = {
                "products_parsed": len(items),
                "filters_loaded": len(all_filters),
                "enabled_filters": len(filters),
                "precheck_hits": 0,
                "final_matches": 0,
                "unavailable_action_skips": 0,
                "missing_target_skips": 0,
                "queued_selenium_jobs": 0,
                "notify_only_actions": 0,
                "unchanged_action_skips": 0,
            }
            if not filters:
                self._log_runtime(
                    "WARNING",
                    "action",
                    ZERO_ENABLED_FILTERS_WARNING,
                    site=site,
                    details=decision_metrics,
                )

            previous_states = self.storage.product_state_map(site, [item.external_id for item in items])
            # Retailer scans are commonly paginated, rate-limited, or routed
            # through partial fallbacks.  Absence is only evidence when a
            # parser explicitly declares that it produced an authoritative
            # complete snapshot.
            reconcile_missing = bool(parser_metrics.get("authoritative_snapshot", False))
            events = self.storage.upsert_items(items, reconcile_missing=reconcile_missing)
            event_types_by_key: dict[tuple[str, str], set[str]] = {}
            for event in events:
                event_types_by_key.setdefault((event.site, event.external_id), set()).add(event.event_type)

            decision_records: dict[tuple[str, str], dict] = {
                (item.site, item.external_id): {
                    "item": item,
                    "matched_rules": [],
                    "skip_reason": "not_evaluated",
                    "notification_queued": False,
                    "selenium_job_queued": False,
                    "action_id": None,
                }
                for item in items
            }

            for event in events:
                item = next(
                    (
                        current
                        for current in items
                        if current.site == event.site and current.external_id == event.external_id
                    ),
                    None,
                )
                matched_rules = [rule for rule in filters if item is not None and match(item, rule)]
                matched_ids = tuple(rule.id for rule in matched_rules)
                self.storage.insert_event(replace(event, matched_filter_ids=matched_ids))

            for item in items:
                decision_key = (item.site, item.external_id)
                decision = decision_records[decision_key]
                if not filters:
                    decision["skip_reason"] = "no_enabled_filters"
                    continue

                prematched = [rule for rule in filters if match_precheck(item, rule)]
                if not prematched:
                    decision["skip_reason"] = "precheck_no_match"
                    continue
                decision_metrics["precheck_hits"] += len(prematched)

                final_item = item
                if item.site == "bol" and "bol" in self.parsers:
                    bol_parser = self.parsers["bol"]
                    try:
                        final_item = await bol_parser.enrich(session, item, self.cfg)
                    except Exception as exc:
                        self._log_runtime(
                            "WARNING",
                            "scan",
                            f"bol enrich failed scan_id={scan_id} external_id={item.external_id} error={exc}",
                            site=item.site,
                            details={"scan_id": scan_id, "external_id": item.external_id},
                        )
                        final_item = item
                decision["item"] = final_item

                matched = [rule for rule in prematched if match(final_item, rule)]
                if not matched:
                    decision["skip_reason"] = "final_filter_no_match"
                    continue
                decision["matched_rules"] = matched
                decision["skip_reason"] = ""
                decision_metrics["final_matches"] += len(matched)

                try:
                    watchlist_item = self.storage.upsert_watchlist_from_item(
                        final_item,
                        matched,
                        source="auto_filter_match",
                    )
                    self._log_runtime(
                        "INFO",
                        "scan",
                        f"auto_watchlist_filter_match scan_id={scan_id} external_id={final_item.external_id} watchlist_id={watchlist_item.get('id')}",
                        site=final_item.site,
                        details={
                            "scan_id": scan_id,
                            "external_id": final_item.external_id,
                            "watchlist_id": watchlist_item.get("id"),
                            "product_key": watchlist_item.get("product_key"),
                            "matched_filter_ids": [rule.id for rule in matched],
                            "matched_filter_names": [rule.name for rule in matched],
                            "availability_status": final_item.extra.get("availability_status") if final_item.extra else None,
                            "availability_confidence": final_item.extra.get("availability_confidence") if final_item.extra else None,
                            "reason": "auto_watchlist_filter_match",
                        },
                    )
                except Exception as exc:
                    self._log_runtime(
                        "WARNING",
                        "scan",
                        f"auto watchlist upsert failed scan_id={scan_id} external_id={final_item.external_id} error={type(exc).__name__}: {exc}",
                        site=final_item.site,
                        details={"scan_id": scan_id, "external_id": final_item.external_id},
                    )

                transition_events = event_types_by_key.get(decision_key, set())
                if not transition_events:
                    decision["skip_reason"] = "no_state_transition"
                    decision_metrics["unchanged_action_skips"] += 1
                    continue

                action_block = self._action_block_reason(final_item)
                if action_block is not None:
                    reason, availability_details = action_block
                    decision["skip_reason"] = reason
                    if "missing_action_target" in reason:
                        decision_metrics["missing_target_skips"] += 1
                    else:
                        decision_metrics["unavailable_action_skips"] += 1
                    matched_rule = matched[0]
                    details = {
                        "event": "action_skipped_unavailable",
                        "scan_id": scan_id,
                        "site": final_item.site,
                        "external_id": final_item.external_id,
                        "title": final_item.title,
                        "url": final_item.url,
                        "filter_id": matched_rule.id,
                        "filter_name": matched_rule.name,
                        "matched_filter_ids": [rule.id for rule in matched],
                        "availability_status": availability_details.get("availability_status"),
                        "availability_text": availability_details.get("availability_text"),
                        "purchasable": False,
                        "reason": reason,
                        "availability_reason": availability_details.get("availability_reason"),
                        "negative_signals": availability_details.get("negative_signals"),
                        "positive_signals": availability_details.get("positive_signals"),
                        "decision": "skip_action_unavailable",
                    }
                    self._log_runtime(
                        "INFO",
                        "action",
                        f"action_skipped_unavailable scan_id={scan_id} external_id={final_item.external_id} reason={reason}",
                        site=final_item.site,
                        details=details,
                    )
                    self.storage.insert_action_log(
                        final_item.site,
                        final_item.external_id,
                        "action_gate",
                        "add_to_cart_and_checkout",
                        "skip_action_unavailable",
                        json.dumps(details, ensure_ascii=False),
                    )
                    continue

                if (
                    self.notifier.is_enabled()
                    and self.cfg.action_mode != "off"
                    and self.cfg.enable_notifications
                    and self.cfg.enable_success_alerts
                    and bool(transition_events & NOTIFICATION_TRANSITIONS)
                ):
                    reasons = "; ".join(explain(final_item, matched[0]))
                    decision["notification_queued"] = True
                    await self.notifier.send(
                        session,
                        f"[{final_item.site}] filter_matched\n"
                        f"{final_item.title}\n"
                        f"{final_item.url}\n"
                        f"price={final_item.price_value}\n"
                        f"available={final_item.is_available}\n"
                        f"reasons={reasons}",
                        metadata={
                            "scan_id": scan_id,
                            "site": final_item.site,
                            "external_id": final_item.external_id,
                            "title": final_item.title,
                            "matched_filter_ids": [rule.id for rule in matched],
                        },
                    )
                    if self.cfg.action_mode == "notify_only":
                        decision_metrics["notify_only_actions"] += 1
                    self._log_runtime(
                        "INFO",
                        "success",
                        f"filter match notified scan_id={scan_id} external_id={final_item.external_id}",
                        site=final_item.site,
                        details={"scan_id": scan_id, "external_id": final_item.external_id},
                    )

                if self.cfg.action_mode == "selenium" and not (transition_events & PURCHASE_TRANSITIONS):
                    decision["skip_reason"] = "no_purchase_transition"
                    continue

                if (
                    self.selenium_dispatcher is not None
                    and self.cfg.action_mode == "selenium"
                ):
                    pocketgames_purchase_key = None
                    if final_item.site == "pocketgames":
                        pocketgames_purchase_key = purchase_key_for_target(final_item.target)
                        existing_purchase = self.storage.get_purchase_state(final_item.site, pocketgames_purchase_key)
                        if existing_purchase and existing_purchase["status"] in BLOCKING_PURCHASE_STATUSES:
                            skip_status = duplicate_skip_status(existing_purchase["status"])
                            reason = f"existing_status={existing_purchase['status']}"
                            decision["skip_reason"] = f"duplicate_{reason}"
                            self.storage.insert_action_log(
                                final_item.site,
                                final_item.external_id,
                                "selenium_queue",
                                "add_to_cart_and_checkout",
                                skip_status,
                                json.dumps(
                                    {
                                        "title": final_item.title,
                                        "purchase_key": pocketgames_purchase_key,
                                        "reason": reason,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                            self._log_runtime(
                                "WARNING",
                                "action",
                                f"pocketgames duplicate blocked scan_id={scan_id} external_id={final_item.external_id} status={skip_status} reason={reason}",
                                site=final_item.site,
                                details={
                                    "scan_id": scan_id,
                                    "external_id": final_item.external_id,
                                    "purchase_key": pocketgames_purchase_key,
                                    "existing_status": existing_purchase["status"],
                                    "status": skip_status,
                                },
                            )
                            await self._send_pocketgames_safety_alert(
                                session,
                                title=final_item.title,
                                status=skip_status,
                                reason=reason,
                            )
                            continue

                    if self.antiban is not None:
                        allowed, reason = self.antiban.allow(final_item.site, "worker")
                        if not allowed:
                            decision["skip_reason"] = f"worker_antiban_{reason}"
                            self._log_runtime(
                                "INFO",
                                "action",
                                f"selenium enqueue skipped scan_id={scan_id} external_id={final_item.external_id} reason={reason}",
                                site=final_item.site,
                                details={"scan_id": scan_id, "external_id": final_item.external_id, "reason": reason},
                            )
                            self.storage.insert_action_log(
                                final_item.site,
                                final_item.external_id,
                                "selenium_queue",
                                "add_to_cart_and_checkout",
                                f"skipped_{reason}",
                                json.dumps({"title": final_item.title}, ensure_ascii=False),
                            )
                            continue

                        self.antiban.mark_op(final_item.site, "worker")
                        if reason == "half_open_probe":
                            self.antiban.use_half_open_probe(final_item.site, "worker")

                    if final_item.site == "pocketgames":
                        if pocketgames_purchase_key is None:
                            pocketgames_purchase_key = purchase_key_for_target(final_item.target)
                        reserved, existing_purchase = self.storage.reserve_purchase_state(
                            site=final_item.site,
                            purchase_key=pocketgames_purchase_key,
                            external_id=final_item.external_id,
                            title=final_item.title,
                            product_url=final_item.url,
                            status=PURCHASE_STATUS_QUEUED,
                            blocking_statuses=BLOCKING_PURCHASE_STATUSES,
                            details={"source": "pipeline_enqueue"},
                        )
                        if not reserved:
                            existing_status = existing_purchase["status"] if existing_purchase else "unknown"
                            skip_status = duplicate_skip_status(existing_status)
                            reason = f"existing_status={existing_status}"
                            decision["skip_reason"] = f"duplicate_{reason}"
                            self.storage.insert_action_log(
                                final_item.site,
                                final_item.external_id,
                                "selenium_queue",
                                "add_to_cart_and_checkout",
                                skip_status,
                                json.dumps(
                                    {
                                        "title": final_item.title,
                                        "purchase_key": pocketgames_purchase_key,
                                        "reason": reason,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                            self._log_runtime(
                                "WARNING",
                                "action",
                                f"pocketgames duplicate blocked at reserve scan_id={scan_id} external_id={final_item.external_id} status={skip_status} reason={reason}",
                                site=final_item.site,
                                details={
                                    "scan_id": scan_id,
                                    "external_id": final_item.external_id,
                                    "purchase_key": pocketgames_purchase_key,
                                    "existing_status": existing_status,
                                    "status": skip_status,
                                },
                            )
                            await self._send_pocketgames_safety_alert(
                                session,
                                title=final_item.title,
                                status=skip_status,
                                reason=reason,
                            )
                            continue

                    job = SeleniumJob(
                        site=final_item.site,
                        case="add_to_cart_and_checkout",
                        target=final_item.target,
                        action_id=f"{final_item.site}-{uuid.uuid4().hex[:8]}",
                        created_at=utc_now_iso(),
                        metadata={
                            "event": "action_created",
                            "scan_id": scan_id,
                            "filter_id": matched[0].id,
                            "filter_name": matched[0].name,
                            "matched_filter_ids": [rule.id for rule in matched],
                            "availability_status": final_item.extra.get("availability_status"),
                            "availability_text": final_item.availability_text,
                            "availability_reason": final_item.extra.get("availability_reason") or final_item.availability_text,
                            "purchasable": final_item.extra.get("purchasable", final_item.is_available),
                            "purchase_key": pocketgames_purchase_key,
                            "source": "parser_scan",
                        },
                    )
                    decision["action_id"] = job.action_id
                    submit_result = self.selenium_dispatcher.submit(job)
                    if submit_result.status == "queued":
                        decision["selenium_job_queued"] = True
                        decision_metrics["queued_selenium_jobs"] += 1
                    else:
                        decision["skip_reason"] = submit_result.status

                    action_created_details = {
                        "event": "action_created",
                        "scan_id": scan_id,
                        "site": final_item.site,
                        "external_id": final_item.external_id,
                        "title": final_item.title,
                        "url": final_item.url,
                        "action_id": job.action_id,
                        "created_at": job.created_at,
                        "job_key": submit_result.job_key,
                        "queue_status": submit_result.status,
                        "purchase_key": pocketgames_purchase_key,
                        "filter_id": matched[0].id,
                        "filter_name": matched[0].name,
                        "matched_filter_ids": [rule.id for rule in matched],
                        "availability_status": final_item.extra.get("availability_status"),
                        "availability_text": final_item.availability_text,
                        "availability_reason": final_item.extra.get("availability_reason") or final_item.availability_text,
                        "purchasable": final_item.extra.get("purchasable", final_item.is_available),
                        "source": "parser_scan",
                    }
                    self.storage.insert_action_log(
                        final_item.site,
                        final_item.external_id,
                        "selenium_queue",
                        "add_to_cart_and_checkout",
                        submit_result.status,
                        json.dumps(action_created_details, ensure_ascii=False),
                    )
                    self._log_runtime(
                        "INFO",
                        "action",
                        f"action_created scan_id={scan_id} site={final_item.site} external_id={final_item.external_id} action_id={job.action_id} status={submit_result.status}",
                        site=final_item.site,
                        details=action_created_details,
                    )
                elif self.cfg.action_mode == "off":
                    decision["skip_reason"] = "action_mode_off"
                elif self.cfg.action_mode == "notify_only" and not decision["notification_queued"]:
                    decision["skip_reason"] = "notification_not_queued"

            for decision in decision_records.values():
                decision_item = decision["item"]
                self._log_scan_decision(
                    scan_id=scan_id,
                    item=decision_item,
                    previous_state=previous_states.get(decision_item.external_id),
                    event_types=event_types_by_key.get((decision_item.site, decision_item.external_id), set()),
                    matched_rules=decision["matched_rules"],
                    skip_reason=decision["skip_reason"],
                    notification_queued=decision["notification_queued"],
                    selenium_job_queued=decision["selenium_job_queued"],
                    action_id=decision["action_id"],
                )

            if events:
                self._log_runtime(
                    "INFO",
                    "success",
                    f"scan completed scan_id={scan_id} status={scan_status} items={len(items)} events={len(events)} duration={time.monotonic() - scan_started:.2f}s",
                    site=site,
                    details={
                        "scan_id": scan_id,
                        "scan_status": scan_status,
                        "items_found": len(items),
                        "events_found": len(events),
                        "page_count": self._page_count(items),
                        "available_count": sum(1 for item in items if item.is_available),
                        "unavailable_count": sum(1 for item in items if not item.is_available),
                        "duration_seconds": round(time.monotonic() - scan_started, 3),
                        "decision_metrics": decision_metrics,
                        "source_mix": parser_metrics.get("source_mix") if parser_metrics else None,
                        "global_cooldown_applied": False,
                        "isolated_backoff_applied": bool(parser_metrics.get("isolated_backoff_applied", False)),
                    },
                )

            self._log_runtime(
                "INFO",
                "scan",
                "filter/action decision metrics",
                site=site,
                details={"scan_id": scan_id, **decision_metrics},
            )

            return SiteRunResult(
                site=site,
                status=scan_status,
                items_found=len(items),
                events_found=len(events),
                message=f"{scan_status} items={len(items)} events={len(events)}",
            )
        except Exception as exc:
            logger.exception("[scan][%s] post-fetch processing failed scan_id=%s", site, scan_id)
            self._log_runtime(
                "ERROR",
                "error",
                f"post-fetch processing failed scan_id={scan_id} type={type(exc).__name__}: {exc}",
                site=site,
                details={"scan_id": scan_id, "error_type": type(exc).__name__},
            )
            return SiteRunResult(site=site, status="error", message=f"processing_failed_{type(exc).__name__}")

    async def _run_once_site(self, session: aiohttp.ClientSession, parser) -> SiteRunResult:
        site = parser.site
        try:
            if self.runtime_state is not None:
                self.runtime_state.mark_scan_started(site)

            async with self.scan_semaphore:
                result = await self.run_site(session, parser)

            cooldown_left = self._parser_cooldown_left(site)
            if self.runtime_state is not None:
                self.runtime_state.mark_scan_result(
                    site,
                    result,
                    next_in_seconds=0.0,
                    cooldown_in_seconds=cooldown_left,
                )
            return result
        except Exception as exc:
            self._log_runtime(
                "ERROR",
                "error",
                f"run-once site task failed type={type(exc).__name__}: {exc}",
                site=site,
            )
            result = SiteRunResult(site=site, status="error", message=f"run_once_failed_{type(exc).__name__}")
            if self.runtime_state is not None:
                self.runtime_state.mark_scan_result(
                    site,
                    result,
                    next_in_seconds=0.0,
                    cooldown_in_seconds=self._parser_cooldown_left(site),
                )
            return result

    async def run_once(self) -> None:
        if not self.parsers:
            self._log_runtime("WARNING", "runtime", "no enabled parsers configured")
            return

        connector = aiohttp.TCPConnector(
            limit=max(20, self.cfg.parser_concurrency * 10),
            ttl_dns_cache=300,
        )
        timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_connect=10, sock_read=40)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as raw_session:
            session = ProxyAwareSession(raw_session, self.cfg)
            tasks = [
                asyncio.create_task(self._run_once_site(session, parser), name=f"scan-once:{parser.site}")
                for parser in self.parsers.values()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for parser, result in zip(self.parsers.values(), results):
            if isinstance(result, Exception):
                self._log_runtime(
                    "ERROR",
                    "error",
                    f"unhandled site task failure type={type(result).__name__}: {result}",
                    site=parser.site,
                )

    async def _site_loop(self, session: aiohttp.ClientSession, parser, scheduler: Scheduler) -> None:
        site = parser.site
        self._log_runtime("INFO", "scheduler", "site loop started", site=site)

        while True:
            try:
                delay = scheduler.sleep_needed(site)
                cooldown_left = self._parser_cooldown_left(site)

                if self.runtime_state is not None:
                    self.runtime_state.mark_waiting(
                        site,
                        next_in_seconds=delay,
                        cooldown_in_seconds=cooldown_left,
                        message="waiting for next scan",
                    )

                if delay > 0:
                    await asyncio.sleep(delay)

                scheduler.mark_run_started(site)

                if self.runtime_state is not None:
                    self.runtime_state.mark_scan_started(site)

                async with self.scan_semaphore:
                    result = await self.run_site(session, parser)

                applied_delay = scheduler.mark_run_finished(site, antiban=self.antiban)
                cooldown_left = self._parser_cooldown_left(site)

                if self.runtime_state is not None:
                    self.runtime_state.mark_scan_result(
                        site,
                        result,
                        next_in_seconds=applied_delay,
                        cooldown_in_seconds=cooldown_left,
                    )

                self._log_runtime(
                    "INFO",
                    "scheduler",
                    f"cycle completed status={result.status} next_in={applied_delay:.2f}s cooldown={cooldown_left:.2f}s",
                    site=site,
                    details={
                        "result_status": result.status,
                        "items_found": result.items_found,
                        "events_found": result.events_found,
                        "next_in_seconds": round(applied_delay, 3),
                        "cooldown_in_seconds": round(cooldown_left, 3),
                        "scheduler": scheduler.snapshot().get(site, {}),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_runtime(
                    "ERROR",
                    "error",
                    f"site loop recovered from unexpected failure type={type(exc).__name__}: {exc}",
                    site=site,
                )
                if self.runtime_state is not None:
                    self.runtime_state.mark_waiting(
                        site,
                        next_in_seconds=1.0,
                        cooldown_in_seconds=self._parser_cooldown_left(site),
                        message="recovering from site loop failure",
                    )
                await asyncio.sleep(1.0)

    async def run_forever(self) -> None:
        if not self.parsers:
            self._log_runtime("WARNING", "runtime", "no enabled parsers configured")
            while True:
                await asyncio.sleep(60)

        scheduler = Scheduler(
            cfg=self.cfg,
            sites=list(self.parsers.keys()),
        )

        connector = aiohttp.TCPConnector(
            limit=max(20, self.cfg.parser_concurrency * 10),
            ttl_dns_cache=300,
        )
        timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_connect=10, sock_read=40)

        self._log_runtime(
            "INFO",
            "runtime",
            f"starting concurrent scan loops enabled_sites={list(self.parsers.keys())} concurrency={self.cfg.parser_concurrency}",
            details={
                "enabled_sites": list(self.parsers.keys()),
                "parser_concurrency": self.cfg.parser_concurrency,
            },
        )

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as raw_session:
            session = ProxyAwareSession(raw_session, self.cfg)
            tasks = [
                asyncio.create_task(self._site_loop(session, parser, scheduler), name=f"scan-loop:{site}")
                for site, parser in self.parsers.items()
            ]
            watchlist_tracker = WatchlistTracker(
                cfg=self.cfg,
                storage=self.storage,
                notifier=self.notifier,
                selenium_dispatcher=self.selenium_dispatcher,
                runtime_state=self.watchlist_runtime_state,
            )
            tasks.append(asyncio.create_task(watchlist_tracker.run_forever(session), name="watchlist-loop"))
            await asyncio.gather(*tasks)
