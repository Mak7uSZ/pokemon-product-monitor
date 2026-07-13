from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import random
import re
import time
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import aiohttp

from pokemon_parser.config import AppConfig
from pokemon_parser.engine.access_control import (
    AccessAssessment,
    AccessOutcome,
    AccessSeverity,
    RecoveryAction,
    SourceAccessController,
    SourceAccessPolicy,
    detect_challenge,
)
from pokemon_parser.models import ActionTarget, AddToCartTarget, CheckoutTarget, ParsedItem
from pokemon_parser.parsers.base import BaseParser
from pokemon_parser.utils.text import clean_text, normalize_text

logger = logging.getLogger(__name__)


class MediaMarktParserDeny(aiohttp.ClientResponseError):
    """
    Explicit parser-level strong deny that must propagate to the pipeline.

    IMPORTANT:
    - a page-local GraphQL error, empty result, timeout, 429, or ambiguous 403 is not enough
    - parser-level deny is raised only for positive challenge/access-block evidence
    """
    pass


class MediaMarktParser(BaseParser):
    site = "mediamarkt"
    category_url = "https://www.mediamarkt.nl/nl/category/pokemon-kaarten-2071.html"
    base_url = "https://www.mediamarkt.nl"
    graphql_url = "https://www.mediamarkt.nl/api/v1/graphql"
    category_pim_code = "CAT_NL_MM_2071"
    graphql_access_source = "mediamarkt.graphql"

    _PRODUCT_HREF_RE = re.compile(
        r'href=(?:"(?P<href1>/nl/product/[^"]+)"|\'(?P<href2>/nl/product/[^\']+)\')',
        flags=re.IGNORECASE,
    )

    _PRICE_TEXT_RE = re.compile(
        r'(?:€|\u20ac|&euro;)\s*'
        r'(?P<int>\d{1,3}(?:[.\s]\d{3})*|\d+)'
        r'(?:\s*[,\.]\s*(?P<frac>\d{2}|[–—-]))?',
        flags=re.IGNORECASE,
    )

    _DEEP_PRICE_KEYS = (
        "displayprice",
        "currentprice",
        "finalprice",
        "salesprice",
        "offerprice",
        "bestprice",
        "mainprice",
        "baseprice",
        "pricewithtax",
        "formattedprice",
        "formattedvalue",
    )

    def __init__(self) -> None:
        self._graphql_access = SourceAccessController()
        self._scan_metrics: dict[str, Any] = self._new_scan_metrics()
        self.last_scan_metrics: dict[str, Any] = self._new_scan_metrics()

    @staticmethod
    def _iso_from_epoch(value: float | None) -> str | None:
        if not value:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def _graphql_circuit_is_open(self) -> bool:
        allowed, _ = self._graphql_access.allow(self.graphql_access_source)
        return not allowed

    def graphql_circuit_snapshot(self) -> dict[str, Any]:
        access = self._graphql_access.snapshot(self.graphql_access_source)
        cooldown_until = access.get("cooldown_until_epoch")
        is_open = bool(cooldown_until and cooldown_until > time.time())
        return {
            "graphql_circuit_open": is_open,
            "graphql_backoff_until": self._iso_from_epoch(cooldown_until) if is_open else None,
            "discovery_routing_mode": "fallback_only" if is_open else "normal",
            "graphql_endpoint_status": "circuit_open" if is_open else access["state"],
            "graphql_access_state": access["state"],
            "graphql_consecutive_quota_denies": access["consecutive_cooldowns"],
            "graphql_soft_deny_occurrences": access["soft_occurrences"],
            "graphql_last_reason_code": access["last_reason_code"],
            "graphql_last_outcome": access["last_outcome"],
        }

    def runtime_endpoint_snapshot(self) -> dict[str, Any]:
        return self.graphql_circuit_snapshot()

    def _new_scan_metrics(self) -> dict[str, Any]:
        snapshot = self.graphql_circuit_snapshot()
        return {
            **snapshot,
            "failure_severity": None,
            "scan_status": "success",
            "source_mix": {},
            "items_fetched": 0,
            "graphql_pages": 0,
            "graphql_partial_successes": 0,
            "graphql_valid_empty_pages": 0,
            "graphql_transient_failures": 0,
            "graphql_soft_denies": 0,
            "graphql_strong_denies": 0,
            "graphql_parse_failures": 0,
            "graphql_outcome_counts": {},
            "graphql_reason_counts": {},
            "html_fallback_pages": 0,
            "html_fallback_pages_with_products": 0,
            "fallback_routing_only_pages": 0,
            "global_cooldown_applied": False,
            "isolated_backoff_applied": False,
            "events": [],
        }

    def _record_graphql_event(self, event: str, *, page: int, **details: Any) -> None:
        payload = {
            "event": event,
            "page": page,
            **details,
            **self.graphql_circuit_snapshot(),
        }
        self._scan_metrics.setdefault("events", []).append(payload)
        logger.info("[mediamarkt] %s page=%s details=%s", event, page, payload)

    def _graphql_policy(self, cfg: AppConfig) -> SourceAccessPolicy:
        threshold_getter = getattr(cfg, "mediamarkt_graphql_soft_deny_escalation_threshold", None)
        window_getter = getattr(cfg, "mediamarkt_graphql_soft_deny_window_seconds", None)
        return SourceAccessPolicy(
            soft_escalation_threshold=(threshold_getter() if callable(threshold_getter) else 3),
            observation_window_seconds=(window_getter() if callable(window_getter) else 300.0),
            base_cooldown_seconds=cfg.mediamarkt_graphql_backoff_seconds(),
            cooldown_multiplier=cfg.mediamarkt_graphql_backoff_multiplier(),
            max_cooldown_seconds=cfg.mediamarkt_graphql_max_backoff_seconds(),
        )

    def _record_graphql_assessment(
        self,
        assessment: AccessAssessment,
        *,
        page: int,
        attempt: int | None = None,
    ) -> None:
        outcome = assessment.outcome.value
        outcome_counts = self._scan_metrics.setdefault("graphql_outcome_counts", {})
        reason_counts = self._scan_metrics.setdefault("graphql_reason_counts", {})
        outcome_counts[outcome] = int(outcome_counts.get(outcome, 0)) + 1
        reason_counts[assessment.reason_code] = int(reason_counts.get(assessment.reason_code, 0)) + 1
        counter_name = {
            AccessOutcome.PARTIAL_SUCCESS: "graphql_partial_successes",
            AccessOutcome.VALID_EMPTY: "graphql_valid_empty_pages",
            AccessOutcome.TRANSIENT_FAILURE: "graphql_transient_failures",
            AccessOutcome.SOFT_DENY: "graphql_soft_denies",
            AccessOutcome.STRONG_DENY: "graphql_strong_denies",
            AccessOutcome.PARSE_FAILURE: "graphql_parse_failures",
        }.get(assessment.outcome)
        if counter_name:
            self._scan_metrics[counter_name] = int(self._scan_metrics.get(counter_name, 0)) + 1
        self._record_graphql_event(
            "graphql_response_classified",
            page=page,
            outcome=outcome,
            reason_code=assessment.reason_code,
            status=assessment.status_code,
            endpoint_category="category_graphql",
            attempt=attempt,
            error_count=assessment.error_count,
            retryable=assessment.retryable,
        )

    def _observe_graphql_assessment(
        self,
        cfg: AppConfig,
        *,
        page: int,
        assessment: AccessAssessment,
        retry_after_seconds: float | None = None,
    ):
        decision = self._graphql_access.observe(
            self.graphql_access_source,
            assessment,
            self._graphql_policy(cfg),
            retry_after_seconds=retry_after_seconds,
        )
        self._record_graphql_event(
            "graphql_recovery_decision",
            page=page,
            outcome=assessment.outcome.value,
            reason_code=assessment.reason_code,
            action=decision.action.value,
            state=decision.state.value,
            occurrence_count=decision.occurrence_count,
            escalated=decision.escalated,
            cooldown_seconds=round(decision.cooldown_seconds, 3),
        )
        if decision.action in {RecoveryAction.COOLDOWN, RecoveryAction.PAUSE_SOURCE}:
            self._scan_metrics["isolated_backoff_applied"] = True
            self._scan_metrics["graphql_backoff_seconds"] = round(decision.cooldown_seconds, 3)
            self._record_graphql_event(
                "graphql_circuit_open",
                page=page,
                status=assessment.status_code,
                reason=assessment.reason_code,
                occurrence_count=decision.occurrence_count,
                escalated=decision.escalated,
                backoff_seconds=round(decision.cooldown_seconds, 3),
                backoff_until=self._iso_from_epoch(decision.cooldown_until),
            )
        return decision

    async def fetch(self, session: aiohttp.ClientSession, cfg: AppConfig) -> list[ParsedItem]:
        self._scan_metrics = self._new_scan_metrics()
        all_items: list[ParsedItem] = []
        global_seen: set[str] = set()
        found_any = False
        max_pages = self.max_pages(cfg) or 20
        page_delay = self.page_delay_seconds(cfg)

        pages_with_any_products = 0
        graphql_soft_denies = 0
        html_fallback_pages = 0
        html_fallback_pages_with_products = 0

        for page in range(1, max_pages + 1):
            logger.info("[mediamarkt] page scan started page=%s", page)

            if page > 1 and page_delay > 0:
                await asyncio.sleep(page_delay)

            page_url = self._build_category_page_url(page)

            try:
                page_sources = await self._fetch_page_sources(
                    session=session,
                    cfg=cfg,
                    page=page,
                    page_url=page_url,
                )
            except MediaMarktParserDeny:
                self._scan_metrics.update(
                    {
                        **self.graphql_circuit_snapshot(),
                        "failure_severity": "critical_channel_failure",
                        "scan_status": "error",
                        "items_fetched": len(all_items),
                        "graphql_soft_denies": graphql_soft_denies,
                        "html_fallback_pages": html_fallback_pages,
                        "html_fallback_pages_with_products": html_fallback_pages_with_products,
                        "global_cooldown_applied": True,
                    }
                )
                self.last_scan_metrics = deepcopy(self._scan_metrics)
                raise

            graphql_data = page_sources["graphql_data"]
            html = page_sources["html"]
            if page_sources["used_html_fallback"]:
                html_fallback_pages += 1
            if page_sources["soft_graphql_deny"]:
                graphql_soft_denies += 1

            raw_products = self._extract_products_from_graphql(graphql_data) if graphql_data else []
            if raw_products:
                logger.info("[mediamarkt] graphql page=%s raw_products=%s", page, len(raw_products))

            if not raw_products and html:
                raw_products = self._extract_products_from_html(html)
                logger.info("[mediamarkt] html page=%s raw_products=%s", page, len(raw_products))

            if page_sources["used_html_fallback"] and raw_products:
                html_fallback_pages_with_products += 1

            if not raw_products:
                if found_any or page >= 3:
                    logger.info("[mediamarkt] stop pagination page=%s reason=no_products", page)
                    break
                continue

            found_any = True
            pages_with_any_products += 1

            item_source = "graphql" if graphql_data is not None else "html_category_fallback"
            page_items = self._build_page_items_from_products(
                raw_products=raw_products,
                page=page,
                global_seen=global_seen,
                source=item_source,
                soft_graphql_deny=bool(page_sources["soft_graphql_deny"]),
            )

            if not page_items and (found_any or page >= 3):
                logger.info("[mediamarkt] stop pagination page=%s reason=no_new_items", page)
                break

            missing_before = sum(1 for item in page_items if item.price_value is None)
            if missing_before > 0:
                logger.info(
                    "[mediamarkt] page=%s missing_prices_before_html=%s",
                    page,
                    missing_before,
                )
                page_items = await self._repair_missing_prices_with_html(
                    session=session,
                    cfg=cfg,
                    page=page,
                    page_url=page_url,
                    html=html,
                    page_items=page_items,
                )

            missing_after = sum(1 for item in page_items if item.price_value is None)
            if missing_after > 0:
                logger.warning(
                    "[mediamarkt] page=%s unresolved_prices_after_html=%s",
                    page,
                    missing_after,
                )

            logger.info("[mediamarkt] parsed page=%s items=%s", page, len(page_items))
            all_items.extend(page_items)

        source_mix: dict[str, int] = {}
        for item in all_items:
            source = str((item.extra or {}).get("source") or "unknown")
            source_mix[source] = source_mix.get(source, 0) + 1

        scan_status = "success"
        failure_severity = None
        degraded_graphql_responses = sum(
            int(self._scan_metrics.get("graphql_outcome_counts", {}).get(outcome, 0))
            for outcome in (
                AccessOutcome.PARTIAL_SUCCESS.value,
                AccessOutcome.TRANSIENT_FAILURE.value,
                AccessOutcome.SOFT_DENY.value,
                AccessOutcome.PARSE_FAILURE.value,
            )
        )
        if graphql_soft_denies and all_items and html_fallback_pages_with_products:
            scan_status = "recovered_via_fallback"
            failure_severity = "endpoint_degraded_recovered"
        elif html_fallback_pages and all_items:
            scan_status = "partial_success"
            failure_severity = "partial_sync_degradation"
        elif degraded_graphql_responses:
            scan_status = "partial_success"
            failure_severity = "endpoint_degraded_no_authoritative_data"

        circuit_snapshot = self.graphql_circuit_snapshot()
        if self._scan_metrics.get("fallback_routing_only_pages", 0) > 0:
            circuit_snapshot["discovery_routing_mode"] = "fallback_only"

        self._scan_metrics.update(
            {
                **circuit_snapshot,
                "failure_severity": failure_severity,
                "scan_status": scan_status,
                "source_mix": source_mix,
                "items_fetched": len(all_items),
                "graphql_soft_denies": graphql_soft_denies,
                "html_fallback_pages": html_fallback_pages,
                "html_fallback_pages_with_products": html_fallback_pages_with_products,
            }
        )
        self.last_scan_metrics = deepcopy(self._scan_metrics)

        logger.info(
            "[mediamarkt] total parsed items=%s pages_with_products=%s html_fallback_pages=%s graphql_soft_denies=%s source_mix=%s status=%s",
            len(all_items),
            pages_with_any_products,
            html_fallback_pages,
            graphql_soft_denies,
            source_mix,
            scan_status,
        )
        return all_items

    async def _fetch_page_sources(
        self,
        session: aiohttp.ClientSession,
        cfg: AppConfig,
        page: int,
        page_url: str,
    ) -> dict[str, Any]:
        """Fetch one page without promoting ambiguous endpoint failures to a global deny."""
        graphql_data: dict[str, Any] | None = None
        html: str | None = None
        soft_graphql_deny = False
        used_html_fallback = False

        if self._graphql_circuit_is_open():
            used_html_fallback = True
            soft_graphql_deny = True
            self._scan_metrics["fallback_routing_only_pages"] = (
                int(self._scan_metrics.get("fallback_routing_only_pages", 0)) + 1
            )
            circuit_snapshot = self.graphql_circuit_snapshot()
            self._record_graphql_event(
                "graphql_circuit_active",
                page=page,
                reason="fallback_routing_only",
                backoff_until=circuit_snapshot.get("graphql_backoff_until"),
            )
        else:
            try:
                assessment = await self._fetch_graphql_page(session, cfg, page)
                if isinstance(assessment, dict):
                    assessment = AccessAssessment(
                        AccessOutcome.SUCCESS,
                        "graphql_legacy_success",
                        payload=assessment,
                        status_code=200,
                    )
            except MediaMarktParserDeny as exc:
                status = getattr(exc, "status", None)
                reason = str(getattr(exc, "message", "") or "")
                if status == 429:
                    assessment = AccessAssessment(
                        AccessOutcome.TRANSIENT_FAILURE,
                        "graphql_http_429",
                        AccessSeverity.DEGRADED,
                        status_code=429,
                        retryable=True,
                    )
                elif reason.startswith("challenge_"):
                    assessment = AccessAssessment(
                        AccessOutcome.STRONG_DENY,
                        reason,
                        AccessSeverity.STRONG,
                        status_code=status,
                    )
                else:
                    assessment = AccessAssessment(
                        AccessOutcome.SOFT_DENY,
                        "graphql_http_403_ambiguous" if status == 403 else "graphql_ambiguous_deny",
                        AccessSeverity.SOFT,
                        status_code=status,
                    )
                self._record_graphql_assessment(assessment, page=page)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                assessment = AccessAssessment(
                    AccessOutcome.TRANSIENT_FAILURE,
                    f"graphql_transport_{type(exc).__name__.lower()}",
                    AccessSeverity.DEGRADED,
                    retryable=True,
                )
                self._record_graphql_assessment(assessment, page=page)

            if not isinstance(assessment, AccessAssessment):
                assessment = AccessAssessment(
                    AccessOutcome.PARSE_FAILURE,
                    "graphql_invalid_classifier_result",
                    AccessSeverity.DEGRADED,
                )
                self._record_graphql_assessment(assessment, page=page)

            decision = self._observe_graphql_assessment(cfg, page=page, assessment=assessment)
            if assessment.outcome in {
                AccessOutcome.SUCCESS,
                AccessOutcome.PARTIAL_SUCCESS,
                AccessOutcome.VALID_EMPTY,
            }:
                graphql_data = assessment.payload
                self._scan_metrics["graphql_pages"] = int(self._scan_metrics.get("graphql_pages", 0)) + 1
                if decision.state.value == "recovered":
                    self._record_graphql_event(
                        "graphql_circuit_closed",
                        page=page,
                        status=assessment.status_code,
                        reason="graphql_probe_success",
                    )
            elif assessment.outcome == AccessOutcome.STRONG_DENY:
                raise MediaMarktParserDeny(
                    request_info=None,
                    history=(),
                    status=assessment.status_code or 403,
                    message=assessment.reason_code,
                    headers=None,
                )
            else:
                soft_graphql_deny = True
                logger.warning(
                    "[mediamarkt] graphql degraded page=%s outcome=%s reason=%s status=%s action=%s -> html fallback",
                    page,
                    assessment.outcome.value,
                    assessment.reason_code,
                    assessment.status_code,
                    decision.action.value,
                )

        if graphql_data is None:
            used_html_fallback = True
            try:
                html = await self._fetch_html(session, cfg, page_url)
            except MediaMarktParserDeny:
                raise
            except aiohttp.ClientResponseError as exc:
                self._record_graphql_event(
                    "html_fallback_failed",
                    page=page,
                    status=exc.status,
                    reason=f"html_http_{exc.status}_ambiguous",
                )
                logger.warning(
                    "[mediamarkt] html fallback failed page=%s status=%s class=%s",
                    page,
                    exc.status,
                    type(exc).__name__,
                )
            except Exception as exc:
                self._record_graphql_event(
                    "html_fallback_failed",
                    page=page,
                    status=None,
                    reason=f"html_transport_{type(exc).__name__.lower()}",
                )
                logger.warning(
                    "[mediamarkt] html fallback failed page=%s class=%s",
                    page,
                    type(exc).__name__,
                )

            if html is not None:
                challenge = detect_challenge(url=page_url, html=html)
                if challenge.detected and challenge.kind is not None and challenge.kind.value != "rate_limited":
                    raise MediaMarktParserDeny(
                        request_info=None,
                        history=(),
                        status=403,
                        message=challenge.reason_code or "html_explicit_challenge",
                        headers=None,
                    )
                if not self._html_has_useful_content(html):
                    self._record_graphql_event(
                        "html_fallback_unparseable",
                        page=page,
                        status=200,
                        reason="html_unparseable_without_challenge_evidence",
                    )
                    html = None

        return {
            "graphql_data": graphql_data,
            "html": html,
            "soft_graphql_deny": soft_graphql_deny,
            "used_html_fallback": used_html_fallback,
            "graphql_outcome": assessment.outcome.value if "assessment" in locals() else "circuit_open",
            "graphql_reason_code": assessment.reason_code if "assessment" in locals() else "graphql_circuit_open",
        }

    def _extract_article_number(self, *values: Any) -> str | None:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text.isdigit() and len(text) >= 5:
                return text
            match = re.search(r"(\d{5,})(?:\.html)?(?:$|[?#])", text)
            if match:
                return match.group(1)
        return None

    def _normalize_external_id(self, product_id: Any, url: str) -> str:
        article_number = self._extract_article_number(product_id, url)
        if article_number:
            return article_number
        candidate = clean_text(str(product_id or url))
        return candidate

    def _source_from_product(self, product: dict[str, Any], default_source: str) -> str:
        raw = product.get("raw") if isinstance(product.get("raw"), dict) else {}
        if raw.get("fallback"):
            return "html_category_fallback"
        return default_source

    def _extract_availability_field(self, node: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
        if depth > 5:
            return None
        if isinstance(node, dict):
            lowered = {str(key).lower(): value for key, value in node.items()}
            for key in keys:
                if key.lower() in lowered:
                    return lowered[key.lower()]
            for value in node.values():
                nested = self._extract_availability_field(value, keys, depth + 1)
                if nested is not None:
                    return nested
        elif isinstance(node, list):
            for item in node:
                nested = self._extract_availability_field(item, keys, depth + 1)
                if nested is not None:
                    return nested
        return None

    def _classify_product_availability(
        self,
        product: dict[str, Any],
        *,
        source: str,
        soft_graphql_deny: bool,
    ) -> dict[str, Any]:
        raw = product.get("raw") if isinstance(product.get("raw"), dict) else {}
        raw_text = json.dumps(raw, ensure_ascii=False).lower()
        in_stock = bool(product.get("in_stock", False))
        delivery_status = self._extract_availability_field(
            raw,
            ("deliveryStatus", "deliveryDisplayStatus", "onlineStatus", "onlineAvailabilityStatus"),
        )
        pickup_status = self._extract_availability_field(
            raw,
            ("pickupStatus", "pickupDisplayStatus", "storeAvailabilityStatus"),
        )
        add_to_cart = bool(
            self._extract_availability_field(raw, ("addToCart", "addToCartAvailable", "isAddToCartAvailable"))
        )
        positive_signals: list[str] = []
        negative_signals: list[str] = []

        if soft_graphql_deny and not in_stock:
            return {
                "status": "rate_limited_unknown",
                "confidence": "low",
                "confidence_score": 0.15,
                "reason": "category GraphQL was rate-limited; HTML fallback cannot prove unavailable",
                "purchasable": False,
                "rate_limited": True,
                "negative_signals": ["graphql_429", "html_category_fallback_low_confidence"],
                "positive_signals": [],
                "raw_delivery_status": delivery_status,
                "raw_pickup_status": pickup_status,
                "add_to_cart_button_exists": add_to_cart,
            }

        if in_stock or add_to_cart:
            positive_signals.append("in_stock_true" if in_stock else "add_to_cart_signal")
            return {
                "status": "add_to_cart_available" if add_to_cart else "delivery_available",
                "confidence": "high",
                "confidence_score": 1.0,
                "reason": "high-confidence product availability signal",
                "purchasable": True,
                "rate_limited": False,
                "negative_signals": negative_signals,
                "positive_signals": positive_signals,
                "raw_delivery_status": delivery_status,
                "raw_pickup_status": pickup_status,
                "add_to_cart_button_exists": add_to_cart,
            }

        if "meldingen activeren" in raw_text or "availability-alert" in raw_text:
            negative_signals.append("notify_button")
            status = "notify_only"
            reason = "notification-only product state"
        elif "binnenkort weer beschikbaar" in raw_text:
            negative_signals.append("soon_available")
            status = "soon_available"
            reason = "soon-available message without purchasable action"
        elif pickup_status and "available" in str(pickup_status).lower() and "not" not in str(pickup_status).lower():
            positive_signals.append("pickup_available")
            status = "pickup_available"
            reason = "pickup availability seen, delivery action target not proven"
        else:
            negative_signals.append("unavailable")
            status = "out_of_stock"
            reason = "no high-confidence purchasable signal"

        confidence = "low" if source == "html_category_fallback" else "medium"
        confidence_score = 0.25 if confidence == "low" else 0.6
        return {
            "status": status,
            "confidence": confidence,
            "confidence_score": confidence_score,
            "reason": reason,
            "purchasable": False,
            "rate_limited": False,
            "negative_signals": negative_signals,
            "positive_signals": positive_signals,
            "raw_delivery_status": delivery_status,
            "raw_pickup_status": pickup_status,
            "add_to_cart_button_exists": add_to_cart,
        }

    def _build_page_items_from_products(
        self,
        raw_products: list[dict[str, Any]],
        page: int,
        global_seen: set[str],
        source: str = "graphql",
        soft_graphql_deny: bool = False,
    ) -> list[ParsedItem]:
        page_items: list[ParsedItem] = []

        for product in raw_products:
            title = clean_text(product.get("title") or "")
            if not title:
                continue

            url = clean_text(product.get("url") or "")
            if not url:
                continue

            product_source = self._source_from_product(product, source)
            external_id = self._normalize_external_id(product.get("product_id"), url)
            if not external_id or external_id in global_seen:
                continue

            title_norm = normalize_text(title)
            price_value = self._coerce_valid_price(product.get("price"))
            raw_meta = product.get("raw", {}) if isinstance(product.get("raw"), dict) else {}
            availability = self._classify_product_availability(
                product,
                source=product_source,
                soft_graphql_deny=soft_graphql_deny,
            )
            is_available = bool(availability["purchasable"])
            availability_text = str(availability["reason"])
            article_number = self._extract_article_number(external_id) or self._extract_article_number(url)

            extra = {
                "sku": product.get("sku"),
                "article_number": article_number,
                "image_url": product.get("image_url"),
                "page": page,
                "source": product_source,
                "availability_source": product_source,
                "price_source": "graphql" if price_value is not None and product_source == "graphql" else None,
                "availability_status": availability["status"],
                "availability_confidence": availability["confidence"],
                "availability_confidence_score": availability["confidence_score"],
                "status_confidence_score": availability["confidence_score"],
                "availability_reason": availability["reason"],
                "purchasable": availability["purchasable"],
                "negative_signals": availability["negative_signals"],
                "positive_signals": availability["positive_signals"],
                "rate_limited": availability["rate_limited"],
                "raw_delivery_status": availability.get("raw_delivery_status"),
                "raw_pickup_status": availability.get("raw_pickup_status"),
                "add_to_cart_button_exists": availability.get("add_to_cart_button_exists"),
            }

            target = None
            if availability["purchasable"] and availability["confidence"] == "high":
                target = ActionTarget(
                    site=self.site,
                    external_id=external_id,
                    title=title,
                    product_url=url,
                    add_to_cart=AddToCartTarget(
                        type="ui_button",
                        quantity=1,
                        product_id=product.get("product_id"),
                        product_url=url,
                        pdp_button_selector='[data-test*="a2c-Button"], [data-test*="add-to-cart"], button[aria-label*="winkelwagen"]',
                    ),
                    checkout=CheckoutTarget(
                        type="ui_flow",
                        cart_url=f"{self.base_url}/nl/checkout",
                        checkout_url=f"{self.base_url}/nl/checkout",
                    ),
                    meta={
                        "sku": product.get("sku"),
                        "article_number": article_number,
                        "image_url": product.get("image_url"),
                        "raw": raw_meta,
                        "page": page,
                        "price_source": extra["price_source"],
                        "availability_status": availability["status"],
                        "availability_confidence": availability["confidence"],
                        "source": product_source,
                    },
                )

            item = ParsedItem(
                site=self.site,
                external_id=external_id,
                title=title,
                title_norm=title_norm,
                url=url,
                price_value=price_value,
                availability_text=availability_text,
                is_available=is_available,
                seller="mediamarkt",
                extra=extra,
                target=target,
            )

            logger.info(
                "[mediamarkt] availability external_id=%s page=%s source=%s status=%s confidence=%s purchasable=%s target=%s reason=%s delivery=%s pickup=%s add_to_cart=%s",
                external_id,
                page,
                product_source,
                availability["status"],
                availability["confidence"],
                availability["purchasable"],
                target is not None,
                availability["reason"],
                availability.get("raw_delivery_status"),
                availability.get("raw_pickup_status"),
                availability.get("add_to_cart_button_exists"),
            )

            page_items.append(item)
            global_seen.add(external_id)

        return page_items

    async def _repair_missing_prices_with_html(
        self,
        session: aiohttp.ClientSession,
        cfg: AppConfig,
        page: int,
        page_url: str,
        html: str | None,
        page_items: list[ParsedItem],
    ) -> list[ParsedItem]:
        if html is None:
            try:
                html = await self._fetch_html(session, cfg, page_url)
            except MediaMarktParserDeny:
                raise
            except Exception as exc:
                logger.warning(
                    "[mediamarkt] html fallback page=%s failed to fetch: %s: %s",
                    page,
                    type(exc).__name__,
                    exc,
                )
                return page_items

        try:
            html_price_index = self._build_price_index_from_html(html)
            logger.info("[mediamarkt] html page=%s price_index=%s", page, len(html_price_index))
        except Exception as exc:
            logger.warning(
                "[mediamarkt] html fallback page=%s failed to build price index: %s: %s",
                page,
                type(exc).__name__,
                exc,
            )
            return page_items

        repaired = 0
        new_items: list[ParsedItem] = []

        for item in page_items:
            if item.price_value is not None:
                new_items.append(item)
                continue

            fallback_price = html_price_index.get(item.url)
            price_source = None

            if fallback_price is not None:
                fallback_price = self._coerce_valid_price(fallback_price)
                price_source = "html_index"

            if fallback_price is None:
                fallback_price = self._extract_price_from_html_near_url(html, item.url)
                fallback_price = self._coerce_valid_price(fallback_price)
                if fallback_price is not None:
                    price_source = "html_near_url"

            if fallback_price is None:
                logger.warning(
                    "[mediamarkt] price unresolved title=%r url=%s page=%s",
                    item.title,
                    item.url,
                    page,
                )
                new_items.append(item)
                continue

            new_extra = dict(item.extra or {})
            new_extra["price_source"] = price_source

            new_target = item.target
            if new_target is not None:
                new_target = replace(
                    new_target,
                    meta={
                        **(new_target.meta or {}),
                        "price_source": price_source,
                    },
                )

            new_item = replace(
                item,
                price_value=fallback_price,
                extra=new_extra,
                target=new_target,
            )

            new_items.append(new_item)
            repaired += 1

        logger.info(
            "[mediamarkt] html fallback page=%s repaired_prices=%s/%s",
            page,
            repaired,
            len(page_items),
        )

        return new_items

    def _build_category_page_url(self, page: int) -> str:
        if page <= 1:
            return self.category_url
        return f"{self.category_url}?page={page}"

    def _classify_graphql_response(self, *, status: int, body: str) -> AccessAssessment:
        parsed: Any = None
        try:
            parsed = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            parsed = None

        if not isinstance(parsed, dict):
            challenge = detect_challenge(html=body, status_code=status)
            if challenge.detected and challenge.kind is not None and challenge.kind.value != "rate_limited":
                return AccessAssessment(
                    AccessOutcome.STRONG_DENY,
                    challenge.reason_code or "graphql_explicit_challenge",
                    AccessSeverity.STRONG,
                    status_code=status,
                    challenge=challenge,
                )
            if status == 429:
                return AccessAssessment(
                    AccessOutcome.TRANSIENT_FAILURE,
                    "graphql_http_429",
                    AccessSeverity.DEGRADED,
                    status_code=status,
                    retryable=True,
                    challenge=challenge,
                )
            if status == 403:
                return AccessAssessment(
                    AccessOutcome.SOFT_DENY,
                    "graphql_http_403_ambiguous",
                    AccessSeverity.SOFT,
                    status_code=status,
                )
            if status in {408, 425} or 500 <= status < 600:
                return AccessAssessment(
                    AccessOutcome.TRANSIENT_FAILURE,
                    f"graphql_http_{status}",
                    AccessSeverity.DEGRADED,
                    status_code=status,
                    retryable=True,
                )
            if 200 <= status < 300 and not body.strip():
                return AccessAssessment(
                    AccessOutcome.TRANSIENT_FAILURE,
                    "graphql_incomplete_empty_response",
                    AccessSeverity.DEGRADED,
                    status_code=status,
                    retryable=True,
                )
            if 200 <= status < 300:
                return AccessAssessment(
                    AccessOutcome.PARSE_FAILURE,
                    "graphql_malformed_json",
                    AccessSeverity.DEGRADED,
                    status_code=status,
                )
            return AccessAssessment(
                AccessOutcome.SOFT_DENY,
                f"graphql_http_{status}_ambiguous",
                AccessSeverity.SOFT,
                status_code=status,
            )

        raw_errors = parsed.get("errors")
        errors = raw_errors if isinstance(raw_errors, list) else []
        error_text_parts: list[str] = []
        for error in errors:
            if not isinstance(error, dict):
                error_text_parts.append(str(error))
                continue
            extensions = error.get("extensions") if isinstance(error.get("extensions"), dict) else {}
            error_text_parts.extend((str(error.get("message") or ""), str(extensions.get("code") or "")))
        error_text = " ".join(error_text_parts).lower()
        data = parsed.get("data")
        products = self._extract_products_from_graphql(parsed)

        def has_expected_collection(node: Any, depth: int = 0) -> bool:
            if depth > 8:
                return False
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key).lower() in {"products", "items", "edges", "nodes", "results"} and isinstance(
                        value, list
                    ):
                        return True
                    if has_expected_collection(value, depth + 1):
                        return True
            elif isinstance(node, list):
                return any(has_expected_collection(value, depth + 1) for value in node)
            return False

        expected_collection = has_expected_collection(data)
        usable_data = bool(products) or expected_collection

        if errors and usable_data:
            return AccessAssessment(
                AccessOutcome.PARTIAL_SUCCESS,
                "graphql_partial_data_with_errors",
                AccessSeverity.DEGRADED,
                status_code=status,
                payload=parsed,
                error_count=len(errors),
            )

        schema_markers = (
            "persistedquerynotfound",
            "persisted query not found",
            "graphql_validation_failed",
            "cannot query field",
            "unknown argument",
            "validation error",
        )
        if errors and any(marker in error_text for marker in schema_markers):
            return AccessAssessment(
                AccessOutcome.PARSE_FAILURE,
                "graphql_schema_or_persisted_query_mismatch",
                AccessSeverity.DEGRADED,
                status_code=status,
                error_count=len(errors),
            )

        transient_markers = (
            "internal_server_error",
            "service unavailable",
            "upstream",
            "gateway timeout",
            "resolver timeout",
        )
        if errors and any(marker in error_text for marker in transient_markers):
            return AccessAssessment(
                AccessOutcome.TRANSIENT_FAILURE,
                "graphql_resolver_transient_failure",
                AccessSeverity.DEGRADED,
                status_code=status,
                retryable=True,
                error_count=len(errors),
            )

        if errors:
            return AccessAssessment(
                AccessOutcome.SOFT_DENY,
                "graphql_error_without_usable_data",
                AccessSeverity.SOFT,
                status_code=status,
                error_count=len(errors),
            )

        if status == 429:
            return AccessAssessment(
                AccessOutcome.TRANSIENT_FAILURE,
                "graphql_http_429",
                AccessSeverity.DEGRADED,
                status_code=status,
                retryable=True,
            )
        if status == 403:
            return AccessAssessment(
                AccessOutcome.SOFT_DENY,
                "graphql_http_403_ambiguous",
                AccessSeverity.SOFT,
                status_code=status,
            )
        if status in {408, 425} or 500 <= status < 600:
            return AccessAssessment(
                AccessOutcome.TRANSIENT_FAILURE,
                f"graphql_http_{status}",
                AccessSeverity.DEGRADED,
                status_code=status,
                retryable=True,
            )
        if not 200 <= status < 300:
            return AccessAssessment(
                AccessOutcome.SOFT_DENY,
                f"graphql_http_{status}_ambiguous",
                AccessSeverity.SOFT,
                status_code=status,
            )
        if data is None:
            return AccessAssessment(
                AccessOutcome.TRANSIENT_FAILURE,
                "graphql_incomplete_empty_data",
                AccessSeverity.DEGRADED,
                status_code=status,
                retryable=True,
            )
        if products:
            return AccessAssessment(
                AccessOutcome.SUCCESS,
                "graphql_products_success",
                AccessSeverity.NONE,
                status_code=status,
                payload=parsed,
            )
        if expected_collection:
            return AccessAssessment(
                AccessOutcome.VALID_EMPTY,
                "graphql_valid_empty_result",
                AccessSeverity.NONE,
                status_code=status,
                payload=parsed,
            )
        return AccessAssessment(
            AccessOutcome.TRANSIENT_FAILURE,
            "graphql_incomplete_response_shape",
            AccessSeverity.DEGRADED,
            status_code=status,
            retryable=True,
        )

    async def _fetch_graphql_page(
        self,
        session: aiohttp.ClientSession,
        cfg: AppConfig,
        page: int,
    ) -> AccessAssessment:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "apollographql-client-name": "pwa-client-pqm",
            "apollographql-client-version": "8.406.0",
            "x-operation": "CategoryV4",
            "x-cacheable": "true",
            "x-mms-language": "nl",
            "x-mms-country": "NL",
            "x-mms-salesline": "Media",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": self._build_category_page_url(page),
        }

        variables = {
            "hasMarketplace": True,
            "isArtificialScarcityActive": True,
            "isCrossLinkingActive": False,
            "shouldIncludeYourekoRatingExp1150": True,
            "locale": "nl-NL",
            "salesLine": "Media",
            "isRefurbishedGoodsActive": True,
            "isPdpFaqSectionActive": True,
            "isDemonstrationModelAvailabilityActive": True,
            "isPdpLoyaltyPointsActive": True,
            "page": page,
            "filters": [],
            "pimCode": self.category_pim_code,
            "searchExperiment": None,
            "criteoInputArgs": {
                "adEnvironment": "desktop",
                "adCustomerId": "4584ecf98d94922367b007fab5e42088e5b418bc",
                "adRetailerVisitorId": "4584ecf98d94922367b007fab5e42088e5b418bc",
                "adOutletId": "742",
                "adGdpr": "1",
            },
            "withPerfChanges": True,
            "cofrConfig": {
                "isEnabled": True,
                "baseDomain": self.base_url,
                "channel": "DESKTOP",
                "isLegacyDataExcluded": False,
                "features": {
                    "badges": {"isFreeShippingBadgeIncluded": False},
                    "crossSalesLine": {"isEnabled": False, "isOutputForced": False},
                    "onlineStatus": {"isPermanentlyNaIndexEnabled": True},
                    "pickup": {"isStrictPickupDisplayStatusEnabled": True},
                    "price": {
                        "strikePriceTypes": [
                            {"strikePriceType": "lop"},
                            {
                                "strikePriceType": "map",
                                "shouldBeStruck": False,
                                "showDiscountBadge": False,
                                "isLegalTextInlineAllowed": True,
                            },
                            {
                                "strikePriceType": "rrp",
                                "shouldBeStruck": False,
                                "showDiscountBadge": False,
                                "isLegalTextInlineAllowed": True,
                            },
                        ],
                        "isBasePriceRequiredFlagRespected": False,
                        "isDiscountLabelEnabled": True,
                        "isDiscountPercentageShown": True,
                        "isDisplayPriceWithStrikePriceRrpThemed": True,
                        "isLongerStrikePricePrefixAllowed": False,
                        "isPromoPriceFiltered": True,
                        "isPromoPriceUsedAsDisplayPriceInApp": False,
                        "isHistoryChartEnabled": False,
                        "discountPercentageMinimum": 5,
                        "discountPercentageMinimumFractionDigits": 0,
                    },
                    "delivery": {
                        "isDeliveryStatusByEarliestDateEnabled": False,
                        "isLocationSourcingEnabled": False,
                    },
                    "refurbishedGoods": {"isEnabled": True},
                },
            },
        }

        params = {
            "operationName": "CategoryV4",
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(
                {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "2e342847548f61a18dac23f58a84301bba5ed4ec36a5a09d6084f827d459b8a3",
                    },
                    "pwa": {
                        "captureChannel": "DESKTOP",
                        "salesLine": "Media",
                        "country": "NL",
                        "language": "nl",
                        "globalLoyaltyProgram": True,
                        "isOneAccountProgramActive": True,
                        "shouldInactiveContractsBeHidden": True,
                        "isCheckoutPhoneCompareActive": True,
                    },
                },
                separators=(",", ":"),
            ),
        }

        retries = self.max_retries(cfg)
        timeout_seconds = self.request_timeout_seconds(cfg)

        for attempt in range(retries + 1):
            try:
                async with session.get(
                    self.graphql_url,
                    headers=headers,
                    params=params,
                    timeout=timeout_seconds,
                ) as resp:
                    body = await resp.text()
                    assessment = self._classify_graphql_response(status=resp.status, body=body)
                    self._record_graphql_assessment(assessment, page=page, attempt=attempt + 1)
                    if assessment.outcome == AccessOutcome.TRANSIENT_FAILURE and attempt < retries:
                        await self.sleep_retry(cfg, attempt)
                        continue
                    return assessment

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                assessment = AccessAssessment(
                    AccessOutcome.TRANSIENT_FAILURE,
                    f"graphql_transport_{type(exc).__name__.lower()}",
                    AccessSeverity.DEGRADED,
                    retryable=True,
                )
                self._record_graphql_assessment(assessment, page=page, attempt=attempt + 1)
                if attempt < retries:
                    await self.sleep_retry(cfg, attempt)
                    continue
                return assessment

        return AccessAssessment(
            AccessOutcome.TRANSIENT_FAILURE,
            "graphql_retry_exhausted",
            AccessSeverity.DEGRADED,
            retryable=False,
        )

    async def _fetch_html(self, session: aiohttp.ClientSession, cfg: AppConfig, url: str) -> str:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        retries = self.max_retries(cfg)
        last_exc: Exception | None = None
        timeout_seconds = self.request_timeout_seconds(cfg)

        for attempt in range(retries + 1):
            try:
                async with session.get(url, headers=headers, timeout=timeout_seconds) as resp:
                    body = await resp.text()
                    if resp.status in (403, 429):
                        challenge = detect_challenge(url=str(resp.url), html=body, status_code=resp.status)
                        if challenge.detected and challenge.kind is not None and challenge.kind.value != "rate_limited":
                            raise MediaMarktParserDeny(
                                request_info=resp.request_info,
                                history=resp.history,
                                status=resp.status,
                                message=challenge.reason_code or "html_explicit_challenge",
                                headers=resp.headers,
                            )
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=f"html_http_{resp.status}_ambiguous",
                            headers=resp.headers,
                        )

                    if resp.status >= 400:
                        resp.raise_for_status()
                    return body

            except MediaMarktParserDeny:
                raise

            except aiohttp.ClientResponseError as exc:
                last_exc = exc
                if not (500 <= exc.status < 600):
                    raise

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc

            if attempt < retries:
                await self.sleep_retry(cfg, attempt)

        assert last_exc is not None
        raise last_exc

    def _html_has_useful_content(self, html: str) -> bool:
        if not html:
            return False

        try:
            products = self._extract_products_from_html(html)
            if products:
                return True
        except Exception:
            pass

        try:
            price_index = self._build_price_index_from_html(html)
            if price_index:
                return True
        except Exception:
            pass

        lowered = html.lower()
        if "/nl/product/" in lowered:
            return True

        return False

    def _extract_pdp_article_number(self, html: str, url: str) -> str | None:
        article_patterns = (
            r'data-test=["\']pdp-article-number["\'][^>]*>(?P<body>.*?)</[^>]+>',
            r'Art\.-Nr\.\s*(?:<!--.*?-->\s*)*(?P<body>\d{5,})',
            r'Artikelnummer\s*:?\s*(?P<body>\d{5,})',
        )
        for pattern in article_patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            body = self._html_to_text(match.group("body"))
            number_match = re.search(r"\b(\d{5,})\b", body)
            if number_match:
                return number_match.group(1)
        return self._extract_article_number(url)

    def _pdp_button_signals(self, html: str) -> dict[str, bool]:
        add_found = False
        add_disabled_states: list[bool] = []
        alert_found = False

        button_tags = re.findall(r"<button\b[^>]*>.*?</button>", html, flags=re.IGNORECASE | re.DOTALL)
        for tag in button_tags:
            attrs = tag.split(">", 1)[0].lower()
            text = self._html_to_text(tag).lower()
            is_alert = "pdp-availability-alert-button" in attrs or "meldingen activeren" in text
            is_add_to_cart = any(
                marker in attrs or marker in text
                for marker in (
                    "pdp-add-to-cart-button",
                    "cofr-add-to-basket-button",
                    "a2c-button",
                    "add-to-cart",
                    "addtobasket",
                    "ik wil bestellen",
                    "in winkelwagen",
                    "toevoegen aan winkelwagen",
                )
            )
            if is_alert:
                alert_found = True
            if not is_alert and is_add_to_cart:
                add_found = True
                disabled = bool(
                    re.search(
                        r'(?:\bdisabled\b|aria-disabled=["\']?true|data-disabled=["\']?true|class=["\'][^"\']*\bdisabled\b)',
                        attrs,
                        flags=re.IGNORECASE,
                    )
                )
                add_disabled_states.append(disabled)

        if not add_found:
            add_marker = re.search(
                r'(?:id=["\']pdp-add-to-cart-button["\']|data-test=["\'][^"\']*(?:cofr-add-to-basket-button|a2c-Button|add-to-cart)[^"\']*["\']|aria-label=["\'][^"\']*Ik wil bestellen[^"\']*["\'])',
                html,
                flags=re.IGNORECASE,
            )
            if add_marker:
                add_found = True
                segment = html[max(0, add_marker.start() - 300): min(len(html), add_marker.end() + 300)]
                add_disabled_states.append(
                    bool(
                        re.search(
                            r'(?:\bdisabled\b|aria-disabled=["\']?true|data-disabled=["\']?true)',
                            segment,
                            flags=re.IGNORECASE,
                        )
                    )
                )

        if not alert_found:
            alert_found = bool(re.search(r'data-test=["\']pdp-availability-alert-button["\']', html, flags=re.IGNORECASE))

        return {
            "add_to_cart_button_found": add_found,
            "add_to_cart_button_disabled": all(add_disabled_states) if add_found and add_disabled_states else False,
            "alert_button_found": alert_found,
        }

    def _pdp_html_signals(self, html: str, url: str) -> dict[str, Any]:
        page_text = self._html_to_text(html).lower()
        delivery_match = re.search(r'data-test=["\']mms-cofr-delivery_([^"\']+)["\']', html, flags=re.IGNORECASE)
        pickup_match = re.search(r'data-test=["\']mms-cofr-pickup_([^"\']+)["\']', html, flags=re.IGNORECASE)
        article_number = self._extract_pdp_article_number(html, url)

        price_block = None
        price_match = re.search(
            r'data-test=["\']mms-product-price["\'][^>]*>(?P<body>.*?)</[^>]+>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if price_match:
            price_block = price_match.group("body")
        price_value = self._choose_best_price(self._html_to_text(price_block or html))

        button_signals = self._pdp_button_signals(html)
        delivery_status = delivery_match.group(1) if delivery_match else None
        pickup_status = pickup_match.group(1) if pickup_match else None
        delivery_status_upper = str(delivery_status or "").upper()

        delivery_available_marker = bool(
            delivery_status_upper == "AVAILABLE"
            or 'data-test="mms-cofr-delivery_AVAILABLE"' in html
            or "online op voorraad" in page_text
            or "voor 23:59 besteld, morgen in huis" in page_text
        )
        delivery_not_available_marker = bool(
            delivery_status_upper == "NOT_AVAILABLE"
            or 'data-test="mms-cofr-delivery_NOT_AVAILABLE"' in html
            or "helaas geen bezorging mogelijk" in page_text
            or "geen bezorging mogelijk" in page_text
        )
        online_status_available_marker = bool(
            re.search(r'data-product-online-status=["\']AVAILABLE["\']', html, flags=re.IGNORECASE)
        )
        soon_available_text_found = "dit product is binnenkort weer beschikbaar" in page_text or "binnenkort weer beschikbaar" in page_text
        notify_text_found = "meldingen activeren" in page_text
        pickup_selector_found = bool(
            str(pickup_status or "").upper() == "NO_STORE_SELECTED"
            or 'data-test="mms-cofr-pickup_NO_STORE_SELECTED"' in html
            or "bekijk de winkelvoorraad voor ophalen" in page_text
            or "selecteer winkel" in page_text
        )
        challenge = detect_challenge(url=url, text=page_text, html=html, source="mediamarkt.pdp")
        challenge_or_blocked_marker = challenge.detected

        return {
            "article_number": article_number,
            "article_number_found": bool(article_number),
            "price_value": price_value,
            "price_found": price_value is not None or 'data-test="mms-product-price"' in html,
            "delivery_available_marker": delivery_available_marker,
            "delivery_not_available_marker": delivery_not_available_marker,
            "online_status_available_marker": online_status_available_marker,
            **button_signals,
            "soon_available_text_found": soon_available_text_found,
            "notify_text_found": notify_text_found,
            "pickup_selector_found": pickup_selector_found,
            "challenge_or_blocked_marker": challenge_or_blocked_marker,
            "challenge_type": challenge.kind.value if challenge.kind else None,
            "challenge_reason_code": challenge.reason_code,
            "challenge_signals": list(challenge.evidence),
            "raw_delivery_status": delivery_status,
            "raw_pickup_status": pickup_status,
        }

    def _classify_pdp_signals(self, signals: dict[str, Any]) -> dict[str, Any]:
        valid_add_to_cart = bool(signals["add_to_cart_button_found"] and not signals["add_to_cart_button_disabled"])
        negative_or_notify = bool(
            signals["delivery_not_available_marker"]
            or signals["alert_button_found"]
            or signals["soon_available_text_found"]
            or signals["notify_text_found"]
            or signals["add_to_cart_button_disabled"]
        )

        if signals["challenge_or_blocked_marker"]:
            return {
                "status": "challenge_or_blocked",
                "confidence": "low",
                "confidence_score": 0.1,
                "purchasable": False,
                "reason": "PDP appears to be a challenge or block page",
            }

        if valid_add_to_cart:
            return {
                "status": "add_to_cart_available",
                "confidence": "high",
                "confidence_score": 1.0,
                "purchasable": True,
                "reason": "PDP add-to-cart control is visible and enabled",
            }

        if (
            not negative_or_notify
            and signals["online_status_available_marker"]
            and signals["delivery_available_marker"]
        ):
            return {
                "status": "delivery_available",
                "confidence": "high",
                "confidence_score": 0.97,
                "purchasable": True,
                "reason": "PDP delivery and online status are available",
            }

        if signals["alert_button_found"] or signals["notify_text_found"]:
            return {
                "status": "notify_only",
                "confidence": "high",
                "confidence_score": 0.9,
                "purchasable": False,
                "reason": "PDP shows notification-only state",
            }

        if signals["soon_available_text_found"]:
            return {
                "status": "soon_available",
                "confidence": "high",
                "confidence_score": 0.85,
                "purchasable": False,
                "reason": "PDP says product is soon available",
            }

        if signals["delivery_not_available_marker"]:
            return {
                "status": "out_of_stock",
                "confidence": "high",
                "confidence_score": 0.8,
                "purchasable": False,
                "reason": "PDP delivery status is not available",
            }

        return {
            "status": "parse_unknown",
            "confidence": "low",
            "confidence_score": 0.2,
            "purchasable": False,
            "reason": "PDP availability could not be classified",
        }

    @staticmethod
    def _pdp_has_conflicting_signals(signals: dict[str, Any]) -> bool:
        positive = bool(
            signals.get("add_to_cart_button_found")
            or signals.get("delivery_available_marker")
            or signals.get("online_status_available_marker")
        )
        negative = bool(
            signals.get("delivery_not_available_marker")
            or signals.get("alert_button_found")
            or signals.get("soon_available_text_found")
            or signals.get("notify_text_found")
        )
        return positive and negative

    def _pdp_diagnostic_from_signals(
        self,
        signals: dict[str, Any],
        *,
        final_status: str,
        confidence: float | str,
        action_target_exists: bool,
    ) -> dict[str, Any]:
        return {
            "article_number_found": bool(signals["article_number_found"]),
            "price_found": bool(signals["price_found"]),
            "delivery_available_marker": bool(signals["delivery_available_marker"]),
            "delivery_not_available_marker": bool(signals["delivery_not_available_marker"]),
            "online_status_available_marker": bool(signals["online_status_available_marker"]),
            "add_to_cart_button_found": bool(signals["add_to_cart_button_found"]),
            "add_to_cart_button_disabled": bool(signals["add_to_cart_button_disabled"]),
            "alert_button_found": bool(signals["alert_button_found"]),
            "soon_available_text_found": bool(signals["soon_available_text_found"]),
            "notify_text_found": bool(signals["notify_text_found"]),
            "pickup_selector_found": bool(signals["pickup_selector_found"]),
            "final_status": final_status,
            "confidence": confidence,
            "action_target_exists": bool(action_target_exists),
            "conflicting_signals": self._pdp_has_conflicting_signals(signals),
        }

    def diagnose_pdp_html(self, html: str, url: str) -> dict[str, Any]:
        signals = self._pdp_html_signals(html, url)
        classification = self._classify_pdp_signals(signals)
        return self._pdp_diagnostic_from_signals(
            signals,
            final_status=str(classification["status"]),
            confidence=float(classification["confidence_score"]),
            action_target_exists=bool(classification["purchasable"]),
        )

    def parse_pdp_html(self, html: str, url: str) -> ParsedItem | None:
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.IGNORECASE | re.DOTALL)
        title = clean_text(self._html_to_text(title_match.group(1))) if title_match else ""
        if not title:
            title = "MediaMarkt product"

        signals = self._pdp_html_signals(html, url)
        classification = self._classify_pdp_signals(signals)
        article_number = signals["article_number"]
        external_id = article_number or self._normalize_external_id(None, url)
        price_value = signals["price_value"]
        delivery_status = signals["raw_delivery_status"]
        pickup_status = signals["raw_pickup_status"]
        add_to_cart_exists = bool(signals["add_to_cart_button_found"])
        notify_button_exists = bool(signals["alert_button_found"] or signals["notify_text_found"])
        soon_available = bool(signals["soon_available_text_found"])

        status = str(classification["status"])
        confidence = str(classification["confidence"])
        confidence_score = float(classification["confidence_score"])
        purchasable = bool(classification["purchasable"])
        reason = str(classification["reason"])

        target = None
        if purchasable:
            target = ActionTarget(
                site=self.site,
                external_id=external_id,
                title=title,
                product_url=url,
                add_to_cart=AddToCartTarget(
                    type="ui_button",
                    quantity=1,
                    product_id=external_id,
                    product_url=url,
                    pdp_button_selector='#pdp-add-to-cart-button, [data-test*="cofr-add-to-basket-button"], [data-test*="a2c-Button"], button[aria-label*="Ik wil bestellen"]',
                ),
                checkout=CheckoutTarget(
                    type="ui_flow",
                    cart_url=f"{self.base_url}/nl/checkout",
                    checkout_url=f"{self.base_url}/nl/checkout",
                ),
                meta={
                    "source": "pdp_html",
                    "availability_status": status,
                    "availability_confidence": confidence,
                    "article_number": article_number,
                    "price_value": price_value,
                    "selector_hints": [
                        "#pdp-add-to-cart-button",
                        '[data-test*="cofr-add-to-basket-button"]',
                        'button text "Ik wil bestellen"',
                    ],
                },
            )

        diagnostic = self._pdp_diagnostic_from_signals(
            signals,
            final_status=status,
            confidence=confidence_score,
            action_target_exists=target is not None,
        )

        return ParsedItem(
            site=self.site,
            external_id=external_id,
            title=title,
            title_norm=normalize_text(title),
            url=url,
            price_value=price_value,
            availability_text=reason,
            is_available=purchasable,
            seller="mediamarkt",
            extra={
                "article_number": article_number,
                "source": "pdp_html",
                "availability_source": "pdp_html",
                "availability_status": status,
                "availability_confidence": confidence,
                "availability_confidence_score": confidence_score,
                "status_confidence_score": confidence_score,
                "availability_reason": reason,
                "purchasable": purchasable,
                "raw_delivery_status": delivery_status,
                "raw_pickup_status": pickup_status,
                "add_to_cart_button_exists": add_to_cart_exists,
                "add_to_cart_button_disabled": bool(signals["add_to_cart_button_disabled"]),
                "notify_button_exists": notify_button_exists,
                "soon_available": soon_available,
                "store_stock_unknown": pickup_status == "NO_STORE_SELECTED",
                "pdp_diagnostic": diagnostic,
                "buyable_marker_found": bool(
                    signals["add_to_cart_button_found"]
                    or signals["delivery_available_marker"]
                    or signals["online_status_available_marker"]
                ),
                "alert_notify_marker_found": bool(
                    signals["alert_button_found"]
                    or signals["notify_text_found"]
                    or signals["soon_available_text_found"]
                ),
                "negative_signals": [] if purchasable else [status],
                "positive_signals": [status] if purchasable else [],
            },
            target=target,
        )

    def _extract_products_from_graphql(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = self._walk_for_products(data)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in candidates:
            key = str(item.get("product_id") or item.get("url") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)

        return result

    def _extract_products_from_html(self, html: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        for blob in self._extract_json_scripts(html):
            candidates.extend(self._walk_for_products(blob))

        if not candidates:
            candidates.extend(self._extract_products_from_links(html))

        result: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in candidates:
            key = str(item.get("product_id") or item.get("url") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)

        return result

    def _extract_json_scripts(self, html: str) -> list[dict[str, Any]]:
        blobs: list[dict[str, Any]] = []

        patterns = [
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>\s*(\{.*?\})\s*</script>',
            r'<script[^>]*type="application/json"[^>]*>\s*(\{.*?\})\s*</script>',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, html, flags=re.DOTALL):
                raw = match.group(1)
                try:
                    blobs.append(json.loads(raw))
                except Exception:
                    continue

        return blobs

    def _walk_for_products(self, data: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def rec(node: Any) -> None:
            if isinstance(node, dict):
                maybe = self._parse_product_node(node)
                if maybe is not None:
                    found.append(maybe)
                for value in node.values():
                    rec(value)
            elif isinstance(node, list):
                for item in node:
                    rec(item)

        rec(data)
        return found

    def _parse_product_node(self, node: dict[str, Any]) -> dict[str, Any] | None:
        possible_id = (
            node.get("id")
            or node.get("productId")
            or node.get("articleNumber")
            or node.get("offerId")
        )
        title = node.get("title") or node.get("name") or node.get("productName")
        url = node.get("url") or node.get("productUrl") or node.get("relativeUrl") or node.get("href")

        if not title or not url:
            return None

        if isinstance(url, str) and url.startswith("/"):
            url = f"{self.base_url}{url}"

        if "pokemon" not in str(title).lower():
            return None

        image_url = (
            node.get("image")
            or node.get("imageUrl")
            or node.get("mainImage")
            or node.get("thumbnail")
        )
        if isinstance(image_url, dict):
            image_url = image_url.get("url")

        return {
            "product_id": str(possible_id) if possible_id is not None else str(url),
            "sku": str(node.get("sku")) if node.get("sku") is not None else None,
            "title": str(title).strip(),
            "url": str(url),
            "image_url": str(image_url) if image_url else None,
            "price": self._extract_price(node),
            "in_stock": self._extract_stock(node),
            "raw": node,
        }

    def _extract_products_from_links(self, html: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        pattern = re.compile(
            r'href="(/nl/product/[^"]+)"[^>]*>(.*?)</a>',
            flags=re.DOTALL | re.IGNORECASE,
        )

        for m in pattern.finditer(html):
            rel_url = m.group(1)
            anchor_html = m.group(2)
            title = re.sub(r"<[^>]+>", " ", anchor_html)
            title = re.sub(r"\s+", " ", title).strip()
            if not title or "pokemon" not in title.lower():
                continue

            abs_url = f"{self.base_url}{rel_url}"
            results.append(
                {
                    "product_id": abs_url,
                    "sku": None,
                    "title": title,
                    "url": abs_url,
                    "image_url": None,
                    "price": None,
                    "in_stock": False,
                    "raw": {"fallback": True},
                }
            )

        return results

    def _extract_price(self, node: dict[str, Any]) -> float | None:
        for value in (
            node.get("displayPrice"),
            node.get("currentPrice"),
            node.get("finalPrice"),
            node.get("salesPrice"),
            node.get("offerPrice"),
            node.get("bestPrice"),
            node.get("mainPrice"),
            node.get("basePrice"),
            node.get("formattedPrice"),
            node.get("price"),
        ):
            price = self._coerce_valid_price(value)
            if price is not None:
                return price

        for container_key in ("priceInfo", "pricing", "priceData", "offer", "seller", "product", "price"):
            container = node.get(container_key)
            if not isinstance(container, dict):
                continue

            for key in (
                "displayPrice",
                "currentPrice",
                "finalPrice",
                "salesPrice",
                "offerPrice",
                "bestPrice",
                "mainPrice",
                "basePrice",
                "priceWithTax",
                "formattedPrice",
                "formattedValue",
                "price",
            ):
                if key in container:
                    price = self._coerce_valid_price(container.get(key))
                    if price is not None:
                        return price

        return self._find_price_deep(node)

    def _find_price_deep(self, obj: Any, depth: int = 0) -> float | None:
        if depth > 6:
            return None

        if isinstance(obj, dict):
            for key, value in obj.items():
                key_l = str(key).lower().replace("_", "")
                if key_l in self._DEEP_PRICE_KEYS:
                    price = self._coerce_valid_price(value)
                    if price is not None:
                        return price

            for value in obj.values():
                nested = self._find_price_deep(value, depth + 1)
                if nested is not None:
                    return nested

        elif isinstance(obj, list):
            for item in obj:
                nested = self._find_price_deep(item, depth + 1)
                if nested is not None:
                    return nested

        return None

    def _extract_price_from_html_near_url(self, html: str | None, product_url: str) -> float | None:
        if not html or not product_url:
            return None

        rel_url = product_url.replace(self.base_url, "")
        match = re.search(
            rf"(.{{0,12000}}{re.escape(rel_url)}.{{0,12000}})",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return None

        text = self._html_to_text(match.group(1))
        return self._choose_best_price(text)

    def _build_price_index_from_html(self, html: str) -> dict[str, float]:
        matches = list(self._PRODUCT_HREF_RE.finditer(html))
        if not matches:
            return {}

        index: dict[str, float] = {}

        for i, match in enumerate(matches):
            rel = match.group("href1") or match.group("href2")
            if not rel:
                continue

            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html), start + 50000)
            segment_text = self._html_to_text(html[start:end])
            price = self._choose_best_price(segment_text)

            if price is None:
                continue

            abs_url = rel if rel.startswith("http") else f"{self.base_url}{rel}"
            index.setdefault(abs_url, price)

        return index

    def _html_to_text(self, blob: str) -> str:
        blob = html_lib.unescape(blob)
        blob = re.sub(r"<script\b[^>]*>.*?</script>", " ", blob, flags=re.IGNORECASE | re.DOTALL)
        blob = re.sub(r"<style\b[^>]*>.*?</style>", " ", blob, flags=re.IGNORECASE | re.DOTALL)
        blob = re.sub(r"<[^>]+>", " ", blob)
        blob = blob.replace("\xa0", " ")
        blob = re.sub(r"\s+", " ", blob).strip()
        return blob

    def _choose_best_price(self, text: str) -> float | None:
        candidates = self._parse_price_candidates_from_text(text)
        candidates = [c for c in candidates if 0 < c < 10000]
        if not candidates:
            return None

        bad_exact = {25.0, 50.0, 100.0}
        preferred = [c for c in candidates if c not in bad_exact]
        if preferred:
            return preferred[0]

        return candidates[0]

    def _parse_price_candidates_from_text(self, text: str) -> list[float]:
        out: list[float] = []

        for m in self._PRICE_TEXT_RE.finditer(text):
            int_part = (m.group("int") or "").replace(" ", "").replace(".", "")
            frac = m.group("frac")

            if not int_part.isdigit():
                continue

            if frac is None:
                frac_digits = "00"
            elif frac.isdigit():
                frac_digits = frac
            else:
                frac_digits = "00"

            try:
                out.append(float(f"{int_part}.{frac_digits}"))
            except ValueError:
                continue

        return out

    def _parse_price_value(self, value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None

        if isinstance(value, (int, float)):
            return float(value)

        if isinstance(value, dict):
            for key in (
                "displayPrice",
                "currentPrice",
                "finalPrice",
                "salesPrice",
                "offerPrice",
                "bestPrice",
                "mainPrice",
                "basePrice",
                "priceWithTax",
                "formattedPrice",
                "formattedValue",
                "price",
                "amount",
                "value",
            ):
                if key in value:
                    parsed = self._parse_price_value(value[key])
                    if parsed is not None:
                        return parsed
            return None

        if isinstance(value, str):
            text = html_lib.unescape(value).replace("\xa0", " ").strip()
            text = text.replace(",-", ",00").replace(",–", ",00").replace(",—", ",00")

            if "€" in text or "&euro;" in text.lower():
                candidates = self._parse_price_candidates_from_text(text)
                if candidates:
                    return candidates[0]

            compact = text.replace("€", "").replace(" ", "")
            if "," in compact and "." in compact:
                compact = compact.replace(".", "").replace(",", ".")
            else:
                compact = compact.replace(",", ".")

            compact = re.sub(r"[^0-9.]", "", compact)

            if compact.count(".") > 1:
                head, tail = compact.rsplit(".", 1)
                compact = f"{head.replace('.', '')}.{tail}"

            if not compact:
                return None

            try:
                return float(compact)
            except ValueError:
                return None

        return None

    def _coerce_valid_price(self, value: Any) -> float | None:
        parsed = self._parse_price_value(value)
        if parsed is None:
            return None
        if parsed <= 0:
            return None
        if parsed > 5000:
            logger.warning("[mediamarkt] suspicious huge parsed price=%s", parsed)
            return None
        return round(parsed, 2)
        
    def _extract_stock(self, node: dict[str, Any]) -> bool:
        # 1. Прямые bool-сигналы на верхнем уровне
        for key in (
            "inStock",
            "available",
            "isAvailable",
            "buyable",
            "isBuyable",
            "orderable",
            "isOrderable",
        ):
            value = node.get(key)
            if isinstance(value, bool):
                return value

        # 2. Вложенные контейнеры со статусом наличия
        for container_key in (
            "stock",
            "availability",
            "shipping",
            "delivery",
            "fulfillment",
            "pickup",
            "onlineAvailability",
        ):
            stock_info = node.get(container_key)
            if not isinstance(stock_info, dict):
                continue

            for key in (
                "inStock",
                "available",
                "isAvailable",
                "buyable",
                "isBuyable",
                "orderable",
                "isOrderable",
            ):
                value = stock_info.get(key)
                if isinstance(value, bool):
                    return value

            for text_key in (
                "status",
                "deliveryStatus",
                "label",
                "message",
                "title",
                "subtitle",
                "description",
                "text",
            ):
                status = stock_info.get(text_key)
                if not isinstance(status, str):
                    continue

                status_l = status.lower()

                # Явные positive сигналы
                if any(x in status_l for x in (
                    "available",
                    "op voorraad",
                    "leverbaar",
                    "direct leverbaar",
                    "morgen in huis",
                    "afhalen mogelijk",
                )):
                    return True

                # Явные negative сигналы
                if any(x in status_l for x in (
                    "unavailable",
                    "niet beschikbaar",
                    "sold out",
                    "uitverkocht",
                    "geen bezorging mogelijk",
                    "binnenkort weer beschikbaar",
                    "meldingen activeren",
                    "zoek naar alternatieven",
                    "niet leverbaar",
                    "tijdelijk niet leverbaar",
                )):
                    return False

        # 3. Глубокий текстовый fallback по всему node
        node_text = json.dumps(node, ensure_ascii=False).lower()

        negative_markers = (
            "helaas geen bezorging mogelijk",
            "geen bezorging mogelijk",
            "dit product is binnenkort weer beschikbaar",
            "binnenkort weer beschikbaar",
            "meldingen activeren",
            "zoek naar alternatieven",
            "niet leverbaar",
            "tijdelijk niet leverbaar",
            "uitverkocht",
            "niet beschikbaar",
            "sold out",
        )
        if any(marker in node_text for marker in negative_markers):
            return False

        positive_markers = (
            "op voorraad",
            "leverbaar",
            "direct leverbaar",
            "morgen in huis",
            "afhalen mogelijk",
            "buyable",
            "\"instock\": true",
            "\"available\": true",
            "\"isavailable\": true",
            "\"isbuyable\": true",
        )
        if any(marker in node_text for marker in positive_markers):
            return True

        # 4. Консервативный дефолт:
        # если наличие не удалось доказать, считаем недоступным
        return False
