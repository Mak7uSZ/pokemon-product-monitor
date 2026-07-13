from __future__ import annotations

import os
import time
import logging
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from pokemon_parser.config import AppConfig
from pokemon_parser.models import ActionTarget
from pokemon_parser.workers.observability import (
    WorkerActionTimeline,
    detect_mediamarkt_unavailable_markers,
    detect_queue_markers,
    driver_context,
    low_level_debug_enabled,
    summarize_alerts_toasts,
    summarize_element,
    summarize_visible_buttons,
    timed_click,
    timed_wait,
)
from pokemon_parser.workers.base import BaseWorkerCase
from pokemon_parser.workers.queue import detect_queue_page, wait_if_queue
from pokemon_parser.workers.timing import WorkerTiming, build_worker_timing
from pokemon_parser.workers.trace import WorkerTraceLogger

logger = logging.getLogger(__name__)


class MediaMarktCheckoutUnavailableError(RuntimeError):
    pass


class MediaMarktWorkerCase(BaseWorkerCase):
    BASE_URL = "https://www.mediamarkt.nl"
    CHECKOUT_URL = f"{BASE_URL}/nl/checkout"

    WAIT_TIMEOUT = 20
    STEP_TIMEOUT = 18
    POLL = 0.2

    SHORT_PAUSE = 0.25
    CLICK_RETRY_PAUSE = 0.45
    POST_CLICK_SETTLE = 0.6

    DEBUG_DIR = Path("debug_artifacts") / "mediamarkt"

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def _apply_timing(cfg: AppConfig) -> WorkerTiming:
        timing = build_worker_timing(cfg)
        MediaMarktWorkerCase.WAIT_TIMEOUT = max(5, int(round(timing.wait_timeout_seconds)))
        MediaMarktWorkerCase.STEP_TIMEOUT = max(5, int(round(timing.wait_timeout_seconds)))
        MediaMarktWorkerCase.POLL = timing.poll_seconds
        MediaMarktWorkerCase.SHORT_PAUSE = timing.click_pause_seconds
        MediaMarktWorkerCase.CLICK_RETRY_PAUSE = timing.retry_pause_seconds
        MediaMarktWorkerCase.POST_CLICK_SETTLE = timing.after_checkout_click_wait_seconds
        return timing

    @staticmethod
    def _wait(driver: WebDriver, timeout: int | None = None) -> WebDriverWait:
        return WebDriverWait(
            driver,
            timeout or MediaMarktWorkerCase.WAIT_TIMEOUT,
            poll_frequency=MediaMarktWorkerCase.POLL,
            ignored_exceptions=(StaleElementReferenceException,),
        )

    @staticmethod
    def _product_url(target: ActionTarget) -> str:
        if not target.product_url:
            raise RuntimeError("mediamarkt: missing product_url")
        return target.product_url

    @staticmethod
    def _debug_page(driver: WebDriver, prefix: str) -> None:
        try:
            print(f"[mediamarkt] {prefix} current_url={driver.current_url}")
        except Exception:
            print(f"[mediamarkt] {prefix} current_url=<unavailable>")

        try:
            print(f"[mediamarkt] {prefix} title={driver.title}")
        except Exception:
            print(f"[mediamarkt] {prefix} title=<unavailable>")

        try:
            print(f"[mediamarkt] {prefix} cookies={len(driver.get_cookies())}")
        except Exception:
            print(f"[mediamarkt] {prefix} cookies=<unavailable>")

    @staticmethod
    def _ensure_debug_dir() -> None:
        os.makedirs(MediaMarktWorkerCase.DEBUG_DIR, exist_ok=True)

    @staticmethod
    def _dump_debug_artifacts(driver: WebDriver, label: str) -> None:
        # Checkout pages can contain addresses, payment data, and session
        # tokens.  Never persist screenshots or full DOM captures from this
        # workflow; the structured worker timeline provides safe diagnostics.
        print(f"[mediamarkt] sensitive checkout artifacts omitted label={label}")

    @staticmethod
    def _wait_dom_settle(driver: WebDriver, timeout: float = 5.0) -> None:
        end = time.time() + timeout
        last_signature = None
        stable_hits = 0

        while time.time() < end:
            try:
                ready = driver.execute_script("return document.readyState")
                count = driver.execute_script("return document.getElementsByTagName('*').length")
                signature = (ready, count)
            except Exception:
                MediaMarktWorkerCase._sleep(0.15)
                continue

            if signature == last_signature and ready in ("interactive", "complete"):
                stable_hits += 1
                if stable_hits >= 3:
                    return
            else:
                stable_hits = 0

            last_signature = signature
            MediaMarktWorkerCase._sleep(0.15)

    @staticmethod
    def _is_visible(el: WebElement) -> bool:
        try:
            return el.is_displayed()
        except StaleElementReferenceException:
            return False
        except Exception:
            return False

    @staticmethod
    def _is_enabled(el: WebElement) -> bool:
        try:
            return el.is_enabled()
        except StaleElementReferenceException:
            return False
        except Exception:
            return False

    @staticmethod
    def _element_has_real_box(el: WebElement) -> bool:
        try:
            rect = el.rect or {}
            return (rect.get("width", 0) or 0) > 0 and (rect.get("height", 0) or 0) > 0
        except Exception:
            return False

    @staticmethod
    def _is_really_clickable_candidate(el: WebElement) -> bool:
        return (
            MediaMarktWorkerCase._is_visible(el)
            and MediaMarktWorkerCase._is_enabled(el)
            and MediaMarktWorkerCase._element_has_real_box(el)
        )

    @staticmethod
    def _collect_button_candidates(driver: WebDriver) -> list[dict]:
        try:
            candidates = driver.execute_script(
                """
                return Array.from(document.querySelectorAll('button,a[role="button"]')).map((el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return {
                        text: (el.innerText || el.textContent || '').trim().slice(0, 120),
                        id: el.id || '',
                        dataTest: el.getAttribute('data-test') || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true',
                        visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none',
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    };
                });
                """
            )
            return list(candidates or [])
        except Exception:
            return []

    @staticmethod
    def _button_candidate_is_buy_intent(candidate: dict) -> bool:
        blob = " ".join(
            str(candidate.get(key) or "").lower()
            for key in ("text", "id", "dataTest", "ariaLabel")
        )
        deny = (
            "meldingen activeren",
            "availability-alert",
            "pdp-availability-alert-button",
            "wishlist",
            "verlanglijst",
            "selecteer winkel",
        )
        if any(marker in blob for marker in deny):
            return False
        allow = (
            "pdp-add-to-cart-button",
            "add-to-basket",
            "cofr-add-to-basket",
            "a2c-button",
            "ik wil bestellen",
            "in winkelwagen",
            "toevoegen aan winkelwagen",
        )
        if any(marker in blob for marker in allow):
            return True
        text = str(candidate.get("text") or "").strip().lower()
        return text == "bestellen"

    @staticmethod
    def _log_button_candidates(driver: WebDriver, trace: WorkerTraceLogger | None = None) -> list[dict]:
        candidates = MediaMarktWorkerCase._collect_button_candidates(driver)
        compact = [
            {
                "text": item.get("text"),
                "id": item.get("id"),
                "data_test": item.get("dataTest"),
                "aria_label": item.get("ariaLabel"),
                "disabled": item.get("disabled"),
                "visible": item.get("visible"),
            }
            for item in candidates
        ]
        logger.info("mediamarkt_button_candidates %s", compact)
        if trace is not None:
            trace.step(
                "mediamarkt_button_candidates",
                {"phase": "add_to_cart", "candidates": compact[:20]},
                level="verbose",
            )
        return candidates

    @staticmethod
    def _record_timeline_event(
        timeline: WorkerActionTimeline | None,
        event_name: str,
        driver: WebDriver | None = None,
        **context,
    ) -> None:
        if timeline is None:
            return
        if driver is not None:
            context = {**driver_context(driver), **context}
        timeline.record(event_name, **context)

    @staticmethod
    def _runtime_log_event(event_name: str, *, action_id: str | None = None, **details) -> None:
        compact = {k: v for k, v in details.items() if v is not None}
        if action_id:
            compact["action_id"] = action_id
        logger.info("%s %s", event_name, compact)

    @staticmethod
    def _fast_queue_check(
        driver: WebDriver,
        *,
        phase: str,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
        timeline: WorkerActionTimeline | None = None,
        timeout_seconds: float = 0.25,
    ) -> str:
        started = time.monotonic()
        action_id = timeline.action_id if timeline is not None else getattr(trace, "action_id", None)
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "queue_check_started",
            driver,
            step_name=f"queue_check_{phase}",
            phase=phase,
            timeout_seconds=timeout_seconds,
        )
        if not getattr(cfg, "queue_check_enabled", True):
            duration_ms = (time.monotonic() - started) * 1000
            MediaMarktWorkerCase._record_timeline_event(
                timeline,
                "queue_check_finished",
                driver,
                step_name=f"queue_check_{phase}",
                phase=phase,
                result="passed",
                duration_ms=duration_ms,
                queue_markers=[],
            )
            return "passed"
        try:
            state = detect_queue_page(driver, "mediamarkt")
            result = "queue_detected" if state.in_queue else "passed"
            duration_ms = (time.monotonic() - started) * 1000
            details = {
                "phase": phase,
                "result": result,
                "duration_ms": round(duration_ms, 3),
                "current_url": state.url,
                "visible_queue_markers": list(state.signals),
            }
            if state.in_queue or phase != "before_warm_add_to_cart":
                MediaMarktWorkerCase._runtime_log_event("queue_check_finished", action_id=action_id, **details)
            MediaMarktWorkerCase._record_timeline_event(
                timeline,
                "queue_check_finished",
                driver,
                step_name=f"queue_check_{phase}",
                **details,
            )
            if state.in_queue:
                if trace is not None:
                    trace.warning(
                        "Queue detected",
                        {"phase": phase, "url": state.url, "signals": list(state.signals)},
                        level="minimal",
                    )
                raise RuntimeError("queue_detected")
            return result
        except RuntimeError:
            raise
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000
            MediaMarktWorkerCase._record_timeline_event(
                timeline,
                "queue_check_finished",
                driver,
                step_name=f"queue_check_{phase}",
                phase=phase,
                result="error",
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return "error"

    @staticmethod
    def fast_revalidate_buy_button(
        driver: WebDriver,
        *,
        target: ActionTarget | None = None,
        timeline: WorkerActionTimeline | None = None,
    ) -> tuple[WebElement | None, dict]:
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "mediamarkt_fast_revalidate_started",
            driver,
            step_name="dom_revalidate",
        )
        started = time.monotonic()
        try:
            payload = driver.execute_script(
                """
                const visiblePageText = ((document.body && document.body.innerText) || '').toLowerCase();
                const html = ((document.documentElement && document.documentElement.innerHTML) || '').toLowerCase();
                const negativePhrases = [
                  'niet langer verkrijgbaar',
                  'niet verkrijgbaar',
                  'helaas geen bezorging mogelijk',
                  'dit product is binnenkort weer beschikbaar'
                ];
                const uniquePush = (rows, value) => {
                  if (value && !rows.includes(value)) rows.push(value);
                };
                const availabilityMarkers = [];
                const structuredNegativeMarkers = [];
                const visibleNegativeMarkers = [];
                const weakNegativeMarkers = negativePhrases.filter((phrase) => html.includes(phrase) || visiblePageText.includes(phrase));
                for (const el of Array.from(document.querySelectorAll('[data-product-online-status]'))) {
                  const status = String(el.getAttribute('data-product-online-status') || '').trim().toLowerCase();
                  if (status === 'available') uniquePush(availabilityMarkers, 'data-product-online-status=AVAILABLE');
                  if (status && status !== 'available' && /(unavailable|not_available|not-available|outofstock|out_of_stock|soldout|sold_out)/.test(status)) {
                    uniquePush(structuredNegativeMarkers, 'data-product-online-status=' + status.toUpperCase());
                  }
                }
                for (const el of Array.from(document.querySelectorAll('[data-test]'))) {
                  const dataTest = String(el.getAttribute('data-test') || '').toLowerCase();
                  if (dataTest.includes('mms-cofr-delivery_available')) {
                    uniquePush(availabilityMarkers, 'data-test=mms-cofr-delivery_AVAILABLE');
                  }
                  if (
                    dataTest.includes('mms-cofr-delivery_not_available') ||
                    dataTest.includes('mms-cofr-delivery_unavailable') ||
                    dataTest.includes('delivery_not_available') ||
                    dataTest.includes('delivery-unavailable') ||
                    dataTest.includes('out-of-stock')
                  ) {
                    uniquePush(structuredNegativeMarkers, 'data-test=' + (el.getAttribute('data-test') || ''));
                  }
                }
                if (html.includes('data-product-online-status="available"')) uniquePush(availabilityMarkers, 'data-product-online-status=AVAILABLE');
                if (html.includes('mms-cofr-delivery_available')) uniquePush(availabilityMarkers, 'data-test=mms-cofr-delivery_AVAILABLE');
                if (visiblePageText.includes('online op voorraad')) uniquePush(availabilityMarkers, 'text=Online op voorraad');
                const isVisible = (el) => {
                  if (!el) return false;
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const scopedSelectors = [
                  '[data-test*="cofr" i]',
                  '[data-test*="delivery" i]',
                  '[data-test*="availability" i]',
                  '[data-test*="buy-box" i]',
                  '[class*="cofr" i]',
                  '[class*="delivery" i]',
                  '[class*="availability" i]',
                  '[class*="buybox" i]',
                  '[class*="buy-box" i]',
                  '[id*="cofr" i]',
                  '[id*="delivery" i]',
                  '[id*="availability" i]',
                  '[id*="buybox" i]',
                  '[id*="buy-box" i]'
                ];
                const scoped = new Set();
                for (const selector of scopedSelectors) {
                  try {
                    for (const el of Array.from(document.querySelectorAll(selector))) scoped.add(el);
                  } catch (err) {}
                }
                for (const el of Array.from(scoped)) {
                  if (!isVisible(el)) continue;
                  const scopedText = String(el.innerText || '').toLowerCase();
                  if (!scopedText) continue;
                  for (const phrase of negativePhrases) {
                    if (scopedText.includes(phrase)) uniquePush(visibleNegativeMarkers, phrase);
                  }
                }
                const buttons = Array.from(document.querySelectorAll(
                  'button[data-test*="cofr-add-to-basket" i],button#pdp-add-to-cart-button,button[data-test*="a2c-Button" i],button,a[role="button"]'
                ));
                const unsafe = [
                  'meldingen activeren',
                  'wishlist',
                  'verlanglijst',
                  'selecteer winkel',
                  'alternatief',
                  'alternatieven',
                  'store selector'
                ];
                const summarize = (el) => {
                  if (!el) return null;
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return {
                    tag: el.tagName.toLowerCase(),
                    text: (el.innerText || el.textContent || '').trim(),
                    id: el.id || '',
                    data_test: el.getAttribute('data-test') || '',
                    aria_label: el.getAttribute('aria-label') || '',
                    class: el.getAttribute('class') || '',
                    visible: isVisible(el),
                    enabled: !Boolean(el.disabled),
                    disabled_attr: el.getAttribute('disabled') || '',
                    aria_disabled: el.getAttribute('aria-disabled') || '',
                    rect: {
                      x: Math.round(rect.x),
                      y: Math.round(rect.y),
                      width: Math.round(rect.width),
                      height: Math.round(rect.height)
                    }
                  };
                };
                const rejectedButtons = [];
                for (const el of buttons) {
                  const summary = summarize(el);
                  const blob = [
                    summary.text,
                    summary.id,
                    summary.data_test,
                    summary.aria_label,
                    summary.class
                  ].join(' ').toLowerCase();
                  const disabled = Boolean(el.disabled) || el.getAttribute('disabled') !== null || el.getAttribute('aria-disabled') === 'true';
                  const hasKnownSelector = summary.id === 'pdp-add-to-cart-button' ||
                    (summary.data_test || '').toLowerCase().includes('cofr-add-to-basket-button') ||
                    (summary.data_test || '').toLowerCase().includes('cofr-add-to-basket') ||
                    (summary.data_test || '').toLowerCase().includes('a2c-button');
                  const hasOrderText = blob.includes('ik wil bestellen') ||
                    blob.includes('in winkelwagen') ||
                    blob.includes('toevoegen aan winkelwagen') ||
                    (summary.text || '').trim().toLowerCase() === 'bestellen';
                  if (!summary.visible) {
                    rejectedButtons.push({reason: 'not_visible', summary});
                    continue;
                  }
                  if (disabled) {
                    rejectedButtons.push({reason: 'disabled', summary});
                    continue;
                  }
                  const unsafeMarker = unsafe.find((marker) => blob.includes(marker));
                  if (unsafeMarker) {
                    rejectedButtons.push({reason: 'unsafe:' + unsafeMarker, summary});
                    continue;
                  }
                  if (hasKnownSelector || hasOrderText) {
                    uniquePush(
                      availabilityMarkers,
                      blob.includes('ik wil bestellen')
                        ? 'button=visible_enabled_Ik wil bestellen'
                        : 'button=visible_enabled_add_to_cart'
                    );
                    const positiveOverride = availabilityMarkers.length > 0;
                    const ignoredNegativeMarkers = positiveOverride
                      ? Array.from(new Set([...weakNegativeMarkers, ...structuredNegativeMarkers, ...visibleNegativeMarkers]))
                      : weakNegativeMarkers.filter((marker) => !structuredNegativeMarkers.includes(marker) && !visibleNegativeMarkers.includes(marker));
                    const availabilityDecision = {
                      decision: 'available',
                      reason: availabilityMarkers.includes('data-test=mms-cofr-delivery_AVAILABLE') && availabilityMarkers.some((marker) => marker.startsWith('button='))
                        ? 'strong_positive_available_delivery_and_visible_enabled_button'
                        : 'visible_enabled_add_to_cart_button',
                      positive_markers: availabilityMarkers,
                      ignored_negative_markers: ignoredNegativeMarkers,
                      ignored_negative_reason: ignoredNegativeMarkers.length
                        ? (positiveOverride ? 'strong_positive_available_signal_override' : 'found_only_in_full_html_or_translation_script')
                        : ''
                    };
                    return {
                      ok: true,
                      reason: 'ready',
                      button: el,
                      button_summary: summary,
                      availability_markers: availabilityMarkers,
                      rejection_markers: [],
                      ignored_negative_markers: ignoredNegativeMarkers,
                      ignored_negative_reason: availabilityDecision.ignored_negative_reason,
                      structured_negative_markers: structuredNegativeMarkers,
                      visible_negative_markers: visibleNegativeMarkers,
                      rejected_buttons: rejectedButtons.slice(0, 10),
                      availability_decision: availabilityDecision
                    };
                  }
                  rejectedButtons.push({reason: 'not_real_buy_button', summary});
                }
                const strongNegativeMarkers = Array.from(new Set([...structuredNegativeMarkers, ...visibleNegativeMarkers]));
                const ignoredNegativeMarkers = weakNegativeMarkers.filter((marker) => !strongNegativeMarkers.includes(marker));
                const hasDisabledOrHiddenBuyButton = rejectedButtons.some((item) => ['not_visible', 'disabled'].includes(item.reason));
                const availabilityDecision = strongNegativeMarkers.length
                  ? {
                      decision: 'unavailable',
                      reason: 'visible_unavailable_status_without_positive_override',
                      positive_markers: availabilityMarkers,
                      rejection_markers: strongNegativeMarkers,
                      ignored_negative_markers: ignoredNegativeMarkers,
                      ignored_negative_reason: ignoredNegativeMarkers.length ? 'found_only_in_full_html_or_translation_script' : ''
                    }
                  : {
                      decision: 'unavailable',
                      reason: hasDisabledOrHiddenBuyButton ? 'no_visible_enabled_add_to_cart_button' : 'buy_button_missing',
                      positive_markers: availabilityMarkers,
                      rejection_markers: [],
                      ignored_negative_markers: ignoredNegativeMarkers,
                      ignored_negative_reason: ignoredNegativeMarkers.length ? 'found_only_in_full_html_or_translation_script' : ''
                    };
                return {
                  ok: false,
                  reason: availabilityDecision.reason,
                  button: null,
                  button_summary: null,
                  availability_markers: availabilityMarkers,
                  rejection_markers: strongNegativeMarkers,
                  ignored_negative_markers: ignoredNegativeMarkers,
                  ignored_negative_reason: availabilityDecision.ignored_negative_reason,
                  structured_negative_markers: structuredNegativeMarkers,
                  visible_negative_markers: visibleNegativeMarkers,
                  rejected_buttons: rejectedButtons.slice(0, 10),
                  availability_decision: availabilityDecision
                };
                """
            ) or {}
        except Exception as exc:
            payload = {
                "ok": False,
                "reason": "revalidation_error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "button": None,
            }

        duration_ms = (time.monotonic() - started) * 1000
        if target is not None:
            try:
                article_match = MediaMarktWorkerCase.article_matches_target(driver, target)
            except Exception:
                article_match = False
            if not article_match:
                payload["ok"] = False
                payload["reason"] = "article_number_mismatch"
                payload["availability_decision"] = {
                    "decision": "unavailable",
                    "reason": "article_number_mismatch",
                    "positive_markers": payload.get("availability_markers") or [],
                    "rejection_markers": payload.get("rejection_markers") or [],
                    "ignored_negative_markers": payload.get("ignored_negative_markers") or [],
                    "ignored_negative_reason": payload.get("ignored_negative_reason") or "",
                }
        button = payload.get("button")
        details = {
            "step_name": "dom_revalidate",
            "result": "success" if payload.get("ok") else "rejected",
            "duration_ms": duration_ms,
            "reason": payload.get("reason"),
            "availability_markers": payload.get("availability_markers") or [],
            "rejection_markers": payload.get("rejection_markers") or [],
            "ignored_negative_markers": payload.get("ignored_negative_markers") or [],
            "ignored_negative_reason": payload.get("ignored_negative_reason") or "",
            "availability_decision": payload.get("availability_decision") or {},
            "button_summary": payload.get("button_summary"),
            "rejected_buttons": payload.get("rejected_buttons") or [],
        }
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "mediamarkt_fast_revalidate_finished",
            driver,
            **details,
        )
        return (button if payload.get("ok") else None), payload

    @staticmethod
    def _wait_for_fast_refresh_markers(
        driver: WebDriver,
        *,
        timeline: WorkerActionTimeline | None,
        timeout: float,
    ) -> None:
        def _has_visible_enabled_buy_button(d: WebDriver) -> bool:
            return MediaMarktWorkerCase._find_enabled_buy_button(d, timeout=0.1) is not None

        if timeline is None:
            WebDriverWait(driver, timeout, poll_frequency=0.1).until(
                lambda d: _has_visible_enabled_buy_button(d)
                or detect_mediamarkt_unavailable_markers(d).get("found")
                or detect_queue_markers(d, "mediamarkt").get("in_queue")
            )
            return
        timed_wait(
            timeline=timeline,
            driver=driver,
            wait_name="optional_refresh_marker_wait",
            condition=lambda d: _has_visible_enabled_buy_button(d)
            or detect_mediamarkt_unavailable_markers(d).get("found")
            or detect_queue_markers(d, "mediamarkt").get("in_queue"),
            timeout=timeout,
            selector_info="visible enabled add-to-cart button,[data-test*='cofr-add-to-basket']",
            poll_frequency=0.1,
        )

    @staticmethod
    def _fast_click_buy_button(
        driver: WebDriver,
        button: WebElement,
        *,
        timeline: WorkerActionTimeline | None = None,
    ) -> None:
        scroll_started = time.monotonic()
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "mediamarkt_fast_button_click_started",
            driver,
            step_name="scroll",
            element=summarize_element(button),
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", button)
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "mediamarkt_fast_button_click_finished",
            driver,
            step_name="scroll",
            duration_ms=(time.monotonic() - scroll_started) * 1000,
            result="success",
        )
        try:
            if timeline is not None:
                timed_click(
                    timeline=timeline,
                    driver=driver,
                    click_name="add_to_cart_click",
                    element=button,
                    method="native",
                    selector_strategy="pdp-add-to-cart-button|cofr-add-to-basket-button",
                    short_delay_seconds=0.03,
                )
            else:
                button.click()
        except (ElementClickInterceptedException, StaleElementReferenceException):
            if timeline is not None:
                timed_click(
                    timeline=timeline,
                    driver=driver,
                    click_name="add_to_cart_click",
                    element=button,
                    method="js",
                    selector_strategy="pdp-add-to-cart-button|cofr-add-to-basket-button",
                    short_delay_seconds=0.03,
                )
            else:
                driver.execute_script("arguments[0].click();", button)

    @staticmethod
    def _wait_add_to_cart_confirmation_fast(
        driver: WebDriver,
        *,
        timeline: WorkerActionTimeline | None,
        timeout: float = 3.0,
    ) -> str:
        def _confirmed(d: WebDriver):
            unavailable = detect_mediamarkt_unavailable_markers(d)
            if unavailable.get("found"):
                return {"result": "unavailable", "markers": unavailable}
            queue = detect_queue_markers(d, "mediamarkt")
            if queue.get("in_queue"):
                return {"result": "queue_detected", "markers": queue}
            try:
                url = d.current_url.lower()
            except Exception:
                url = ""
            try:
                has_checkout = bool(
                    d.find_elements(By.XPATH, MediaMarktWorkerCase._contains_text_xpath("Bekijk winkelwagen"))
                    or d.find_elements(By.XPATH, MediaMarktWorkerCase._contains_text_xpath("Ik ga bestellen"))
                )
            except Exception:
                has_checkout = False
            if "checkout" in url or has_checkout:
                return {"result": "confirmed"}
            return False

        try:
            if timeline is not None:
                result = timed_wait(
                    timeline=timeline,
                    driver=driver,
                    wait_name="add_to_cart_confirmation_ms",
                    condition=_confirmed,
                    timeout=timeout,
                    selector_info="checkout markers",
                    poll_frequency=0.1,
                )
            else:
                result = WebDriverWait(driver, timeout, poll_frequency=0.1).until(_confirmed)
        except TimeoutException:
            MediaMarktWorkerCase._record_timeline_event(
                timeline,
                "add_to_cart_confirmation_timeout",
                driver,
                step_name="add_to_cart_confirmation_ms",
                result="timeout",
            )
            return "timeout"
        if isinstance(result, dict):
            return str(result.get("result") or "confirmed")
        return "confirmed"

    @staticmethod
    def _find_enabled_buy_button(driver: WebDriver, timeout: float = 2.0) -> WebElement | None:
        end = time.time() + max(0.1, timeout)
        while time.time() < end:
            try:
                element = driver.execute_script(
                    """
                    const deny = ['meldingen activeren', 'availability-alert', 'pdp-availability-alert-button', 'wishlist', 'verlanglijst', 'selecteer winkel'];
                    const allow = ['pdp-add-to-cart-button', 'add-to-basket', 'cofr-add-to-basket', 'a2c-button', 'ik wil bestellen', 'in winkelwagen', 'toevoegen aan winkelwagen'];
                    const buttons = Array.from(document.querySelectorAll('button,a[role="button"]'));
                    for (const el of buttons) {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        const visible = rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                        const disabled = Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true';
                        const blob = [
                            el.innerText || el.textContent || '',
                            el.id || '',
                            el.getAttribute('data-test') || '',
                            el.getAttribute('aria-label') || ''
                        ].join(' ').toLowerCase();
                        if (!visible || disabled) continue;
                        if (deny.some((marker) => blob.includes(marker))) continue;
                        if (allow.some((marker) => blob.includes(marker)) || (el.innerText || '').trim().toLowerCase() === 'bestellen') {
                            return el;
                        }
                    }
                    return null;
                    """
                )
                if element is not None:
                    return element
            except Exception:
                pass
            MediaMarktWorkerCase._sleep(min(0.15, MediaMarktWorkerCase.POLL))
        return None

    @staticmethod
    def _extract_visible_article_number(driver: WebDriver) -> str:
        try:
            value = driver.execute_script(
                """
                const article = document.querySelector('[data-test="pdp-article-number"]');
                const text = article ? article.innerText : document.body.innerText;
                const match = String(text || '').match(/(?:Art\\.-Nr\\.|Artikelnummer)?\\s*(\\d{5,})/i);
                return match ? match[1] : '';
                """
            )
            return str(value or "")
        except Exception:
            return ""

    @staticmethod
    def article_matches_target(driver: WebDriver, target: ActionTarget) -> bool:
        expected = str(target.external_id or "").strip()
        if not expected:
            return True
        try:
            current_url = driver.current_url or ""
        except Exception:
            current_url = ""
        if expected in current_url:
            return True
        article = MediaMarktWorkerCase._extract_visible_article_number(driver)
        return bool(article and article == expected)

    @staticmethod
    def detect_pdp_dom_status(driver: WebDriver) -> str:
        try:
            payload = driver.execute_script(
                """
                const text = (document.body && document.body.innerText ? document.body.innerText : '').toLowerCase();
                const html = document.documentElement ? document.documentElement.innerHTML.toLowerCase() : '';
                const hasVisible = (needle) => text.includes(needle);
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const buyButton = (() => {
                    const deny = ['meldingen activeren', 'availability-alert', 'pdp-availability-alert-button', 'wishlist', 'verlanglijst'];
                    const allow = ['pdp-add-to-cart-button', 'add-to-basket', 'cofr-add-to-basket', 'a2c-button', 'ik wil bestellen', 'in winkelwagen'];
                    for (const el of Array.from(document.querySelectorAll('button,a[role="button"]'))) {
                        const disabled = Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true';
                        const blob = [
                            el.innerText || el.textContent || '',
                            el.id || '',
                            el.getAttribute('data-test') || '',
                            el.getAttribute('aria-label') || ''
                        ].join(' ').toLowerCase();
                        if (!visible(el) || disabled) continue;
                        if (deny.some((marker) => blob.includes(marker))) continue;
                        if (allow.some((marker) => blob.includes(marker))) return true;
                    }
                    return false;
                })();
                const scopedSelectors = [
                    '[data-test*="cofr" i]',
                    '[data-test*="delivery" i]',
                    '[data-test*="availability" i]',
                    '[data-test*="buy-box" i]',
                    '[class*="cofr" i]',
                    '[class*="delivery" i]',
                    '[class*="availability" i]',
                    '[class*="buybox" i]',
                    '[class*="buy-box" i]',
                    '[id*="cofr" i]',
                    '[id*="delivery" i]',
                    '[id*="availability" i]',
                    '[id*="buybox" i]',
                    '[id*="buy-box" i]'
                ];
                const scopedText = [];
                const seen = new Set();
                for (const selector of scopedSelectors) {
                    try {
                        for (const el of Array.from(document.querySelectorAll(selector))) {
                            if (seen.has(el) || !visible(el)) continue;
                            seen.add(el);
                            const value = (el.innerText || '').trim().toLowerCase();
                            if (value) scopedText.push(value);
                        }
                    } catch (err) {}
                }
                const scoped = scopedText.join('\\n');
                const dataTests = Array.from(document.querySelectorAll('[data-test]'))
                    .map((el) => String(el.getAttribute('data-test') || '').toLowerCase());
                const productStatuses = Array.from(document.querySelectorAll('[data-product-online-status]'))
                    .map((el) => String(el.getAttribute('data-product-online-status') || '').trim().toLowerCase());
                const structuredDeliveryUnavailable = dataTests.some((value) =>
                    value.includes('mms-cofr-delivery_not_available') ||
                    value.includes('mms-cofr-delivery_unavailable') ||
                    value.includes('delivery_not_available')
                );
                const visibleDeliveryUnavailable = scoped.includes('helaas geen bezorging mogelijk') ||
                    scoped.includes('niet verkrijgbaar');
                return {
                    buyButton,
                    onlineAvailable: productStatuses.includes('available') || hasVisible('online op voorraad'),
                    deliveryAvailable: dataTests.some((value) => value.includes('mms-cofr-delivery_available')),
                    deliveryNotAvailable: structuredDeliveryUnavailable || visibleDeliveryUnavailable,
                    outOfStock: html.includes('schema.org/outofstock') || hasVisible('outofstock'),
                    soon: scoped.includes('dit product is binnenkort weer beschikbaar') || scoped.includes('binnenkort weer beschikbaar'),
                    notify: scoped.includes('meldingen activeren')
                };
                """
            ) or {}
        except Exception:
            return "unknown"

        if payload.get("buyButton"):
            return "buyable"
        if payload.get("soon"):
            return "soon_available"
        if payload.get("notify"):
            return "notify_only"
        if payload.get("deliveryNotAvailable") or payload.get("outOfStock"):
            return "out_of_stock"
        if payload.get("onlineAvailable") and payload.get("deliveryAvailable"):
            return "buyable"
        return "unknown"

    @staticmethod
    def _find_visible_elements(driver: WebDriver, locator: tuple[str, str]) -> list[WebElement]:
        elements = driver.find_elements(*locator)
        return [el for el in elements if MediaMarktWorkerCase._is_visible(el)]

    @staticmethod
    def _has_visible(driver: WebDriver, locator: tuple[str, str]) -> bool:
        return len(MediaMarktWorkerCase._find_visible_elements(driver, locator)) > 0

    @staticmethod
    def _scroll_into_view(driver: WebDriver, el: WebElement) -> None:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            el,
        )
        MediaMarktWorkerCase._sleep(MediaMarktWorkerCase.SHORT_PAUSE)

    @staticmethod
    def _click_element(driver: WebDriver, el: WebElement) -> None:
        MediaMarktWorkerCase._scroll_into_view(driver, el)
        try:
            el.click()
        except (ElementClickInterceptedException, StaleElementReferenceException):
            driver.execute_script("arguments[0].click();", el)

    @staticmethod
    def _exact_text_xpath(text: str) -> str:
        return (
            f"//button[normalize-space()='{text}']"
            f"|//button[.//span[normalize-space()='{text}']]"
            f"|//a[normalize-space()='{text}']"
            f"|//a[.//span[normalize-space()='{text}']]"
        )

    @staticmethod
    def _contains_text_xpath(text: str) -> str:
        return (
            f"//button[contains(normalize-space(.), '{text}')]"
            f"|//button[.//span[contains(normalize-space(.), '{text}')]]"
            f"|//a[contains(normalize-space(.), '{text}')]"
            f"|//a[.//span[contains(normalize-space(.), '{text}')]]"
        )

    @staticmethod
    def _has_text_button(driver: WebDriver, text: str) -> bool:
        return MediaMarktWorkerCase._has_visible(
            driver,
            (By.XPATH, MediaMarktWorkerCase._exact_text_xpath(text)),
        )

    @staticmethod
    def _find_best_exact_text_button(
        driver: WebDriver,
        text: str,
        timeout: int = 10,
    ) -> WebElement:
        container_locator = (By.ID, "continueButtonWrapper")
        local_xpath = (
            f".//button[normalize-space()='{text}']"
            f"|.//button[.//span[normalize-space()='{text}']]"
            f"|.//a[normalize-space()='{text}']"
            f"|.//a[.//span[normalize-space()='{text}']]"
        )
        global_xpath = MediaMarktWorkerCase._exact_text_xpath(text)

        def _locate(d: WebDriver):
            containers = d.find_elements(*container_locator)

            for container in containers:
                try:
                    candidates = container.find_elements(By.XPATH, local_xpath)
                except StaleElementReferenceException:
                    continue

                visible = [
                    el for el in candidates
                    if MediaMarktWorkerCase._is_really_clickable_candidate(el)
                ]
                if visible:
                    return visible[-1]

            candidates = d.find_elements(By.XPATH, global_xpath)
            visible = [
                el for el in candidates
                if MediaMarktWorkerCase._is_really_clickable_candidate(el)
            ]
            if visible:
                return visible[-1]

            return False

        return MediaMarktWorkerCase._wait(driver, timeout).until(_locate)

    @staticmethod
    def _try_click_exact_text(driver: WebDriver, text: str, label: str, attempts: int = 4) -> bool:
        last_exc = None

        for attempt in range(1, attempts + 1):
            try:
                print(f"[mediamarkt] trying {label} by exact text={text!r} attempt={attempt}")
                el = MediaMarktWorkerCase._find_best_exact_text_button(driver, text, timeout=10)

                try:
                    print(
                        "[mediamarkt] exact candidate "
                        f"label={label} displayed={el.is_displayed()} enabled={el.is_enabled()} rect={el.rect}"
                    )
                    print(f"[mediamarkt] exact candidate text={el.text!r}")
                except Exception:
                    pass

                MediaMarktWorkerCase._click_element(driver, el)
                print(f"[mediamarkt] success {label} by exact text={text!r} attempt={attempt}")
                return True
            except Exception as exc:
                last_exc = exc
                print(
                    f"[mediamarkt] failed {label} by exact text={text!r} "
                    f"attempt={attempt} error={type(exc).__name__}: {exc}"
                )
                MediaMarktWorkerCase._sleep(MediaMarktWorkerCase.CLICK_RETRY_PAUSE)

        print(f"[mediamarkt] exact text click failed label={label} text={text!r} last_exc={last_exc}")
        return False

    @staticmethod
    def _try_click_locators(
        driver: WebDriver,
        locators: list[tuple[str, str]],
        label: str,
        attempts: int = 3,
        timeout: int = 8,
    ) -> bool:
        last_exc = None

        for locator in locators:
            for attempt in range(1, attempts + 1):
                try:
                    print(f"[mediamarkt] trying {label} locator={locator} attempt={attempt}")
                    el = MediaMarktWorkerCase._wait(driver, timeout).until(
                        EC.element_to_be_clickable(locator)
                    )
                    MediaMarktWorkerCase._click_element(driver, el)
                    print(f"[mediamarkt] success {label} locator={locator} attempt={attempt}")
                    return True
                except Exception as exc:
                    last_exc = exc
                    print(
                        f"[mediamarkt] failed {label} locator={locator} "
                        f"attempt={attempt} error={type(exc).__name__}: {exc}"
                    )
                    MediaMarktWorkerCase._sleep(MediaMarktWorkerCase.CLICK_RETRY_PAUSE)

        print(f"[mediamarkt] locator click failed label={label} last_exc={last_exc}")
        return False

    @staticmethod
    def _try_click_js_query(driver: WebDriver, selectors: list[str], label: str) -> bool:
        for selector in selectors:
            try:
                print(f"[mediamarkt] trying js {label} selector={selector}")
                clicked = driver.execute_script(
                    """
                    const selector = arguments[0];
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    el.scrollIntoView({block: 'center', inline: 'center'});
                    el.click();
                    return true;
                    """,
                    selector,
                )
                if clicked:
                    print(f"[mediamarkt] success js {label} selector={selector}")
                    return True
            except Exception as exc:
                print(
                    f"[mediamarkt] failed js {label} selector={selector} "
                    f"error={type(exc).__name__}: {exc}"
                )
        return False

    @staticmethod
    def _wait_for_condition(
        driver: WebDriver,
        label: str,
        predicate: Callable[[WebDriver], bool],
        timeout: int = STEP_TIMEOUT,
    ) -> None:
        def _wrapped(d: WebDriver) -> bool:
            try:
                return bool(predicate(d))
            except StaleElementReferenceException:
                return False
            except Exception:
                return False

        print(f"[mediamarkt] waiting for condition label={label}")
        MediaMarktWorkerCase._wait(driver, timeout).until(_wrapped)
        MediaMarktWorkerCase._sleep(MediaMarktWorkerCase.POST_CLICK_SETTLE)
        MediaMarktWorkerCase._wait_dom_settle(driver, timeout=4.0)
        MediaMarktWorkerCase._debug_page(driver, f"after {label}")

    @staticmethod
    def _click_with_confirm(
        driver: WebDriver,
        *,
        text: str,
        label: str,
        confirm: Callable[[WebDriver], bool],
        fallback_locators: list[tuple[str, str]] | None = None,
        fallback_js_selectors: list[str] | None = None,
        timeout: int = 15,
    ) -> None:
        clicked = MediaMarktWorkerCase._try_click_exact_text(driver, text, label=label, attempts=4)

        if not clicked and fallback_locators:
            clicked = MediaMarktWorkerCase._try_click_locators(
                driver,
                fallback_locators,
                label=label,
                attempts=2,
                timeout=6,
            )

        if not clicked and fallback_js_selectors:
            clicked = MediaMarktWorkerCase._try_click_js_query(driver, fallback_js_selectors, label=label)

        if not clicked:
            raise RuntimeError(f"mediamarkt: step button not clicked label={label} text={text!r}")

        MediaMarktWorkerCase._wait_for_condition(driver, label, confirm, timeout=timeout)

    @staticmethod
    def _detect_checkout_step(driver: WebDriver) -> str:
        if MediaMarktWorkerCase._has_text_button(driver, "Bekijk winkelwagen"):
            return "drawer"

        if MediaMarktWorkerCase._has_text_button(driver, "Ik ga bestellen"):
            return "step_ik_ga_bestellen"

        if MediaMarktWorkerCase._has_text_button(driver, "Verder"):
            return "step_verder"

        if MediaMarktWorkerCase._has_text_button(driver, "Doorgaan en betalen"):
            return "step_doorgaan_en_betalen"

        if MediaMarktWorkerCase._has_visible(driver, (By.CSS_SELECTOR, "input#CRECA")):
            return "payment"

        return "unknown"

    @staticmethod
    def _log_detected_step(driver: WebDriver, prefix: str) -> str:
        step = MediaMarktWorkerCase._detect_checkout_step(driver)
        print(f"[mediamarkt] {prefix} detected_step={step}")
        return step
    
    @staticmethod
    def _page_text(driver: WebDriver) -> str:
        try:
            text = driver.execute_script(
                "return (document.body && document.body.innerText) ? document.body.innerText : '';"
            )
            return (text or "").strip().lower()
        except Exception:
            try:
                return (driver.page_source or "").lower()
            except Exception:
                return ""

    @staticmethod
    def _detect_checkout_step_soft(driver: WebDriver) -> str:
        # 1. Самые точные сигналы сначала
        if MediaMarktWorkerCase._is_external_payment_page(driver):
            return "external_payment"

        if MediaMarktWorkerCase._has_text_button(driver, "Bekijk winkelwagen"):
            return "drawer"

        if MediaMarktWorkerCase._has_text_button(driver, "Ik ga bestellen"):
            return "step_ik_ga_bestellen"

        if MediaMarktWorkerCase._has_text_button(driver, "Verder"):
            return "step_verder"

        if MediaMarktWorkerCase._has_text_button(driver, "Doorgaan en betalen"):
            return "step_doorgaan_en_betalen"

        if MediaMarktWorkerCase._has_visible(driver, (By.CSS_SELECTOR, "input#CRECA")):
            return "payment"

        # 2. Fallback по видимому тексту страницы, осторожно
        text = MediaMarktWorkerCase._page_text(driver)

        if "bekijk winkelwagen" in text:
            return "drawer"

        if "ik ga bestellen" in text:
            return "step_ik_ga_bestellen"

        if "doorgaan en betalen" in text:
            return "step_doorgaan_en_betalen"

        # "Verder" слишком короткое слово, поэтому ловим только если checkout уже открыт
        try:
            current_url = driver.current_url.lower()
        except Exception:
            current_url = ""

        if "checkout" in current_url and "\nverder\n" in f"\n{text}\n":
            return "step_verder"

        if "checkout" in current_url and (
            "creditcard" in text
            or "betaling" in text
            or "betaalmethode" in text
        ):
            return "payment"

        return "unknown"

    @staticmethod
    def _resolve_checkout_step_stable(
        driver: WebDriver,
        timeout: int = 18,
        stable_hits_required: int = 2,
    ) -> str:
        print("[mediamarkt] resolving checkout step (stable)...")
        end = time.time() + timeout
        last_step = None
        stable_hits = 0

        while time.time() < end:
            MediaMarktWorkerCase._wait_dom_settle(driver, timeout=1.2)
            step = MediaMarktWorkerCase._detect_checkout_step_soft(driver)

            if step != "unknown" and step == last_step:
                stable_hits += 1
                if stable_hits >= stable_hits_required:
                    print(f"[mediamarkt] stable checkout step resolved={step}")
                    return step
            else:
                stable_hits = 0

            last_step = step
            MediaMarktWorkerCase._sleep(0.35)

        final_step = MediaMarktWorkerCase._detect_checkout_step_soft(driver)
        print(f"[mediamarkt] stable checkout step timeout final_step={final_step}")
        return final_step

    @staticmethod
    def _wait_checkout_actionable(driver: WebDriver, timeout: int = 18) -> str:
        print("[mediamarkt] waiting for checkout to become actionable...")

        def _probe(d: WebDriver):
            step = MediaMarktWorkerCase._detect_checkout_step_soft(d)
            if step != "unknown":
                return step
            return False

        try:
            MediaMarktWorkerCase._wait(driver, timeout).until(_probe)
        except Exception:
            pass

        MediaMarktWorkerCase._sleep(MediaMarktWorkerCase.POST_CLICK_SETTLE)
        MediaMarktWorkerCase._wait_dom_settle(driver, timeout=3.0)
        return MediaMarktWorkerCase._resolve_checkout_step_stable(driver, timeout=6)

    @staticmethod
    def _checkout_debug_snapshot(driver: WebDriver, label: str) -> None:
        MediaMarktWorkerCase._debug_page(driver, label)
        MediaMarktWorkerCase._dump_debug_artifacts(driver, label)

    @staticmethod
    def _checkout_state_snapshot(driver: WebDriver) -> dict:
        markers = detect_mediamarkt_unavailable_markers(driver)
        try:
            headings = driver.execute_script(
                """
                return Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]'))
                  .map((el) => (el.innerText || el.textContent || '').trim())
                  .filter(Boolean)
                  .slice(0, 20);
                """
            ) or []
        except Exception:
            headings = []
        try:
            cart = driver.execute_script(
                """
                const text = ((document.body && document.body.innerText) || '');
                const total = text.match(/(?:totaal|total)\\s*[:\\n ]+([^\\n]+)/i);
                const count = text.match(/(\\d+)\\s+(?:artikel|artikelen|item|items)/i);
                return {
                  cart_total: total ? total[1].trim().slice(0, 80) : '',
                  cart_item_count: count ? count[1] : ''
                };
                """
            ) or {}
        except Exception:
            cart = {}
        if not isinstance(cart, dict):
            cart = {}
        return {
            "unavailable_markers_found": markers.get("markers") or [],
            "disabled_checkout_button_markers": markers.get("disabled_checkout_buttons") or [],
            "cart_total": cart.get("cart_total") or "",
            "cart_item_count": cart.get("cart_item_count") or "",
            "visible_headings": headings,
            "visible_alerts_toasts": summarize_alerts_toasts(driver),
            "visible_button_summaries": summarize_visible_buttons(driver, limit=30),
        }

    @staticmethod
    def _log_checkout_state(
        driver: WebDriver,
        *,
        step_name: str,
        trace: WorkerTraceLogger | None = None,
        timeline: WorkerActionTimeline | None = None,
    ) -> dict:
        state = MediaMarktWorkerCase._checkout_state_snapshot(driver)
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "mediamarkt_checkout_state",
            driver,
            step_name=step_name,
            checkout_state=state,
        )
        if state.get("unavailable_markers_found") or state.get("disabled_checkout_button_markers"):
            action_id = timeline.action_id if timeline is not None else getattr(trace, "action_id", None)
            MediaMarktWorkerCase._runtime_log_event(
                "mediamarkt_checkout_unavailable_detected",
                action_id=action_id,
                step_name=step_name,
                unavailable_markers_found=state.get("unavailable_markers_found"),
                disabled_checkout_button_markers=state.get("disabled_checkout_button_markers"),
            )
        return state

    @staticmethod
    def _raise_if_checkout_unavailable(
        driver: WebDriver,
        *,
        step_name: str,
        trace: WorkerTraceLogger | None = None,
        timeline: WorkerActionTimeline | None = None,
    ) -> None:
        state = MediaMarktWorkerCase._log_checkout_state(
            driver,
            step_name=step_name,
            trace=trace,
            timeline=timeline,
        )
        if not state.get("unavailable_markers_found") and not state.get("disabled_checkout_button_markers"):
            return
        if trace is not None:
            trace.set_result(
                "out_of_stock_after_cart",
                {
                    "phase": "checkout",
                    "step_name": step_name,
                    "unavailable_markers_found": state.get("unavailable_markers_found"),
                    "disabled_checkout_button_markers": state.get("disabled_checkout_button_markers"),
                },
            )
            trace.warning(
                "MediaMarkt checkout unavailable detected",
                {
                    "phase": "checkout",
                    "step_name": step_name,
                    "status": "out_of_stock_after_cart",
                },
                level="minimal",
            )
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "mediamarkt_checkout_unavailable_detected",
            driver,
            step_name=step_name,
            result="out_of_stock_after_cart",
            checkout_state=state,
        )
        raise MediaMarktCheckoutUnavailableError(f"out_of_stock_after_cart: {step_name}")

    @staticmethod
    def _is_external_payment_page(driver: WebDriver) -> bool:
        try:
            parsed = urlsplit(str(driver.current_url or "").strip())
        except (AttributeError, ValueError):
            return False
        hostname = (parsed.hostname or "").rstrip(".").lower()
        is_computop = hostname == "computop-paygate.com" or hostname.endswith(".computop-paygate.com")
        return parsed.scheme.lower() == "https" and is_computop

    @staticmethod
    def _fast_fill(driver: WebDriver, css_selector: str, value: str) -> bool:
        if not value:
            return False

        try:
            el = driver.find_element(By.CSS_SELECTOR, css_selector)
        except Exception:
            print(f"[mediamarkt] fast_fill skip selector={css_selector} reason=not_found")
            return False

        try:
            driver.execute_script(
                """
                const el = arguments[0];
                const value = arguments[1];

                el.scrollIntoView({block: 'center', inline: 'center'});
                el.focus();
                el.value = value;

                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                """,
                el,
                value,
            )
            print(f"[mediamarkt] fast_fill success selector={css_selector}")
            return True
        except Exception as exc:
            print(f"[mediamarkt] fast_fill failed selector={css_selector} error={type(exc).__name__}: {exc}")
            return False


    @staticmethod
    def _react_fast_fill(driver: WebDriver, css_selector: str, value: str) -> bool:
        if not value:
            return False

        try:
            el = driver.find_element(By.CSS_SELECTOR, css_selector)
        except Exception:
            print(f"[mediamarkt] react_fast_fill skip selector={css_selector} reason=not_found")
            return False

        try:
            driver.execute_script(
                """
                const el = arguments[0];
                const value = arguments[1];

                el.scrollIntoView({block: 'center', inline: 'center'});
                el.focus();

                // V2.0: Обход React 16+ Synthetic Events
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                if (nativeInputValueSetter) {
                    nativeInputValueSetter.call(el, value);
                } else {
                    el.value = value;
                }

                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                """,
                el,
                value,
            )
            print(f"[mediamarkt] react_fast_fill success selector={css_selector}")
            return True
        except Exception as exc:
            print(f"[mediamarkt] react_fast_fill failed selector={css_selector} error={type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _fill_checkout_contact_fields(driver: WebDriver, cfg: AppConfig) -> None:
        MediaMarktWorkerCase._fast_fill(driver, "input#email", getattr(cfg, "checkout_email", ""))
        MediaMarktWorkerCase._fast_fill(driver, 'input[name="firstName"]', getattr(cfg, "checkout_first_name", ""))
        MediaMarktWorkerCase._fast_fill(driver, 'input[name="lastName"]', getattr(cfg, "checkout_last_name", ""))
        MediaMarktWorkerCase._fast_fill(driver, 'input[name="streetName"]', getattr(cfg, "checkout_street", ""))
        MediaMarktWorkerCase._fast_fill(driver, 'input[name="streetNumber"]', getattr(cfg, "checkout_house_number", ""))
        MediaMarktWorkerCase._fast_fill(driver, 'input[name="postalCode"]', getattr(cfg, "checkout_zip_code", ""))
        MediaMarktWorkerCase._fast_fill(driver, 'input[name="city"]', getattr(cfg, "checkout_city", ""))

    @staticmethod
    def _click_add_to_cart(driver: WebDriver, target: ActionTarget | None = None, trace: WorkerTraceLogger | None = None) -> None:
        MediaMarktWorkerCase._debug_page(driver, "before add_to_cart")

        MediaMarktWorkerCase._log_button_candidates(driver, trace)
        if target is not None and not MediaMarktWorkerCase.article_matches_target(driver, target):
            raise RuntimeError("mediamarkt: current PDP article number does not match target")

        dom_status = MediaMarktWorkerCase.detect_pdp_dom_status(driver)
        if dom_status in {"out_of_stock", "soon_available", "notify_only"}:
            if trace is not None:
                trace.set_result("mediamarkt_warm_action_stale_stock", {"dom_status": dom_status, "phase": "add_to_cart"})
            logger.warning("mediamarkt_warm_action_stale_stock dom_status=%s", dom_status)
            raise RuntimeError(f"mediamarkt_warm_action_stale_stock: {dom_status}")

        button = MediaMarktWorkerCase._find_enabled_buy_button(driver, timeout=2.0)
        clicked = False
        if button is not None:
            logger.info("mediamarkt_buy_button_found")
            if trace is not None:
                trace.step("mediamarkt_buy_button_found", {"phase": "add_to_cart"})
            MediaMarktWorkerCase._click_element(driver, button)
            logger.info("mediamarkt_buy_button_clicked")
            clicked = True

        if not clicked:
            clicked = MediaMarktWorkerCase._try_click_exact_text(
                driver,
                "Ik wil bestellen",
                "add_to_cart",
                attempts=2,
            )

        if not clicked:
            clicked = MediaMarktWorkerCase._try_click_locators(
                driver,
                [
                    (By.CSS_SELECTOR, "button#pdp-add-to-cart-button"),
                    (By.CSS_SELECTOR, '[data-test*="cofr-add-to-basket"]'),
                    (By.CSS_SELECTOR, '[data-test*="a2c-Button"]'),
                    (By.CSS_SELECTOR, 'button[data-test*="add-to-basket"]'),
                    (By.XPATH, MediaMarktWorkerCase._contains_text_xpath("Ik wil bestellen")),
                    (By.XPATH, MediaMarktWorkerCase._contains_text_xpath("In winkelwagen")),
                    (By.XPATH, MediaMarktWorkerCase._contains_text_xpath("Bestellen")),
                ],
                label="add_to_cart",
                attempts=2,
                timeout=4,
            )

        if not clicked:
            clicked = MediaMarktWorkerCase._try_click_js_query(
                driver,
                [
                    "button#pdp-add-to-cart-button",
                    'button[data-test="cofr-add-to-basket-button a2c-Button"]',
                    '[data-test*="cofr-add-to-basket"]',
                    '[data-test*="add-to-basket"]',
                    '[data-test*="a2c-Button"]',
                ],
                label="add_to_cart",
            )

        if not clicked:
            raise RuntimeError("mediamarkt: add to cart button not clicked")

        MediaMarktWorkerCase._wait_for_condition(
            driver,
            "add_to_cart",
            lambda d: (
                MediaMarktWorkerCase._has_text_button(d, "Bekijk winkelwagen")
                or "checkout" in d.current_url.lower()
                or MediaMarktWorkerCase._has_text_button(d, "Ik ga bestellen")
            ),
            timeout=12,
        )

    @staticmethod
    def _click_drawer_checkout(driver: WebDriver) -> None:
        clicked = MediaMarktWorkerCase._try_click_exact_text(
            driver,
            "Bekijk winkelwagen",
            label="open_cart_or_checkout",
            attempts=4,
        )

        if not clicked:
            clicked = MediaMarktWorkerCase._try_click_locators(
                driver,
                [
                    (By.CSS_SELECTOR, 'a[data-test="mms-router-link-mms-pre-checkout-primary-button"]'),
                    (By.XPATH, MediaMarktWorkerCase._contains_text_xpath("Bekijk winkelwagen")),
                    (By.XPATH, "//a[contains(@href, '/checkout')]"),
                ],
                label="open_cart_or_checkout",
                attempts=2,
                timeout=6,
            )

        if not clicked:
            clicked = MediaMarktWorkerCase._try_click_js_query(
                driver,
                [
                    'a[data-test="mms-router-link-mms-pre-checkout-primary-button"]',
                ],
                label="open_cart_or_checkout",
            )

        if not clicked:
            MediaMarktWorkerCase._checkout_debug_snapshot(driver, "drawer_click_failed")
            raise RuntimeError("mediamarkt: step button not clicked label='open_cart_or_checkout' text='Bekijk winkelwagen'")

        print("[mediamarkt] waiting checkout navigation after drawer click")
        MediaMarktWorkerCase._wait(driver, 15).until(
            lambda d: "checkout" in d.current_url.lower() or MediaMarktWorkerCase._is_external_payment_page(d)
        )
        MediaMarktWorkerCase._sleep(MediaMarktWorkerCase.POST_CLICK_SETTLE)
        MediaMarktWorkerCase._wait_dom_settle(driver, timeout=4.0)
        MediaMarktWorkerCase._debug_page(driver, "after open_cart_or_checkout")

    @staticmethod
    def _click_step_ik_ga_bestellen(driver: WebDriver) -> None:
        MediaMarktWorkerCase._wait(driver, 10).until(
            lambda d: MediaMarktWorkerCase._has_visible(d, (By.ID, "continueButtonWrapper"))
        )

        MediaMarktWorkerCase._click_with_confirm(
            driver,
            text="Ik ga bestellen",
            label="ik_ga_bestellen",
            confirm=lambda d: (
                MediaMarktWorkerCase._has_text_button(d, "Verder")
                or MediaMarktWorkerCase._has_text_button(d, "Doorgaan en betalen")
                or MediaMarktWorkerCase._has_visible(d, (By.CSS_SELECTOR, "input#CRECA"))
            ),
            fallback_locators=[
                (
                    By.XPATH,
                    "//*[@id='continueButtonWrapper']"
                    "//button[@data-test='checkout-continue-button'][.//span[normalize-space()='Ik ga bestellen']]",
                ),
                (
                    By.XPATH,
                    "//*[@id='continueButtonWrapper']"
                    "//button[.//span[normalize-space()='Ik ga bestellen']]",
                ),
                (
                    By.CSS_SELECTOR,
                    "#continueButtonWrapper button[data-test='checkout-continue-button']",
                ),
            ],
            fallback_js_selectors=[
                "#continueButtonWrapper button[data-test='checkout-continue-button']",
                "div[data-test='checkout-continue-desktop-enabled'] button[data-test='checkout-continue-button']",
            ],
            timeout=15,
        )

    @staticmethod
    def _click_step_verder(driver: WebDriver) -> None:
        MediaMarktWorkerCase._wait(driver, 10).until(
            lambda d: MediaMarktWorkerCase._has_visible(d, (By.ID, "continueButtonWrapper"))
        )

        MediaMarktWorkerCase._click_with_confirm(
            driver,
            text="Verder",
            label="verder",
            confirm=lambda d: (
                MediaMarktWorkerCase._has_text_button(d, "Doorgaan en betalen")
                or MediaMarktWorkerCase._has_visible(d, (By.CSS_SELECTOR, "input#CRECA"))
            ),
            fallback_locators=[
                (
                    By.XPATH,
                    "//*[@id='continueButtonWrapper']"
                    "//button[@data-test='checkout-continue-button'][.//span[normalize-space()='Verder']]",
                ),
                (
                    By.XPATH,
                    "//*[@id='continueButtonWrapper']"
                    "//button[.//span[normalize-space()='Verder']]",
                ),
                (
                    By.CSS_SELECTOR,
                    "#continueButtonWrapper button[data-test='checkout-continue-button']",
                ),
            ],
            fallback_js_selectors=[
                "#continueButtonWrapper button[data-test='checkout-continue-button']",
                "div[data-test='checkout-continue-desktop-enabled'] button[data-test='checkout-continue-button']",
            ],
            timeout=15,
        )

    @staticmethod
    def _click_step_doorgaan_en_betalen(driver: WebDriver) -> None:
        MediaMarktWorkerCase._wait(driver, 10).until(
            lambda d: MediaMarktWorkerCase._has_visible(d, (By.ID, "continueButtonWrapper"))
        )

        MediaMarktWorkerCase._click_with_confirm(
            driver,
            text="Doorgaan en betalen",
            label="doorgaan_en_betalen",
            confirm=lambda d: (
                MediaMarktWorkerCase._is_external_payment_page(d)
                or MediaMarktWorkerCase._has_visible(d, (By.CSS_SELECTOR, "input#CRECA"))
                or "creditcard" in d.page_source.lower()
                or "betaling" in d.page_source.lower()
            ),
            fallback_locators=[
                (
                    By.XPATH,
                    "//*[@id='continueButtonWrapper']"
                    "//button[@data-test='checkout-continue-button'][.//span[normalize-space()='Doorgaan en betalen']]",
                ),
                (
                    By.XPATH,
                    "//*[@id='continueButtonWrapper']"
                    "//button[.//span[normalize-space()='Doorgaan en betalen']]",
                ),
                (
                    By.CSS_SELECTOR,
                    "#continueButtonWrapper button[data-test='checkout-continue-button']",
                ),
            ],
            fallback_js_selectors=[
                "#continueButtonWrapper button[data-test='checkout-continue-button']",
                "div[data-test='checkout-continue-desktop-enabled'] button[data-test='checkout-continue-button']",
            ],
            timeout=20,
        )

    @staticmethod
    def _try_select_creditcard(driver: WebDriver) -> bool:
        return MediaMarktWorkerCase._try_click_locators(
            driver,
            [
                (By.CSS_SELECTOR, "input#CRECA"),
                (By.CSS_SELECTOR, 'label[for="CRECA"]'),
                (By.XPATH, "//label[contains(normalize-space(.), 'Creditcard')]"),
            ],
            label="select_creditcard",
            attempts=2,
            timeout=5,
        )
    
    @staticmethod
    def _fill_card_data(driver: WebDriver, cfg: AppConfig) -> None:
        print("[mediamarkt] waiting for external payment fields...")
        started = time.time()
        
        # Ждем появления инпута карты
        while time.time() - started < 15:
            if driver.find_elements(By.CSS_SELECTOR, 'input#cardNumber'):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("mediamarkt: external payment fields not ready")

        # Если в cfg expiry идет в формате "1225", React-маска обычно сама ставит "/" 
        # при правильном dispatchEvent. 
        card_fields = [
            ('input#cardNumber', getattr(cfg, "checkout_card_number", "")),
            ('input#expMonth', getattr(cfg, "checkout_card_expiry", "")),
            ('input#checkNumber', getattr(cfg, "checkout_card_cvv", "")),
            ('input#cardholderName', getattr(cfg, "checkout_card_name", "")),
        ]

        for input_sel, value in card_fields:
            MediaMarktWorkerCase._react_fast_fill(driver, input_sel, value)
            MediaMarktWorkerCase._sleep(0.3)

    @staticmethod
    def _click_pay(driver: WebDriver) -> None:
        try:
            print("[mediamarkt] clicking pay button on external page")
            # Даем React секунду на валидацию формы и снятие атрибута disabled
            MediaMarktWorkerCase._sleep(1.0) 
            
            driver.execute_script("""
                const btn = document.querySelector('button.pay-btn');
                if (btn) {
                    btn.removeAttribute('disabled'); // Форсируем разблокировку на всякий случай
                    btn.scrollIntoView({block: 'center', inline: 'center'});
                    btn.click();
                }
            """)
        except Exception as e:
            print(f"[mediamarkt] external pay button click failed: {e}")

    @staticmethod
    def add_to_cart(
        driver: WebDriver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        timing = MediaMarktWorkerCase._apply_timing(cfg)
        product_url = MediaMarktWorkerCase._product_url(target)

        print(f"[mediamarkt] product_url={product_url}")
        if trace is not None:
            trace.step("Opening product page", {"phase": "product_page", "url": product_url})
        driver.get(product_url)
        MediaMarktWorkerCase._wait_dom_settle(driver, timeout=4.5)
        MediaMarktWorkerCase._sleep(timing.after_navigation_wait_seconds)
        MediaMarktWorkerCase._debug_page(driver, "after product open")
        wait_if_queue(driver, site="mediamarkt", phase="after product page open", cfg=cfg, trace=trace)

        if trace is not None:
            trace.step("Searching add-to-cart button", {"phase": "add_to_cart"})
        wait_if_queue(driver, site="mediamarkt", phase="before add_to_cart", cfg=cfg, trace=trace)
        MediaMarktWorkerCase._click_add_to_cart(driver, target=target, trace=trace)
        if trace is not None:
            trace.step("Clicked add-to-cart", {"phase": "add_to_cart", "url": driver.current_url})
        MediaMarktWorkerCase._sleep(timing.after_add_to_cart_wait_seconds)
        wait_if_queue(driver, site="mediamarkt", phase="after add_to_cart", cfg=cfg, trace=trace)

    @staticmethod
    def add_to_cart_from_current_page(
        driver: WebDriver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
        timeline: WorkerActionTimeline | None = None,
        job_received_monotonic: float | None = None,
        warm_tab_switched_monotonic: float | None = None,
        hot_path_started_monotonic: float | None = None,
    ) -> None:
        MediaMarktWorkerCase._apply_timing(cfg)
        if low_level_debug_enabled(cfg):
            MediaMarktWorkerCase._debug_page(driver, "warm add_to_cart start")
        if trace is not None:
            trace.step("Searching add-to-cart button", {"phase": "add_to_cart", "action_path": "warm_tab"}, level="verbose")
        MediaMarktWorkerCase._fast_queue_check(
            driver,
            phase="before_warm_add_to_cart",
            cfg=cfg,
            trace=trace,
            timeline=timeline,
            timeout_seconds=0.25,
        )
        if low_level_debug_enabled(cfg):
            MediaMarktWorkerCase._log_button_candidates(driver, trace)

        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "mediamarkt_fast_button_find_started",
            driver,
            step_name="button_find_ms",
            selector="button#pdp-add-to-cart-button,[data-test*='cofr-add-to-basket-button']",
        )
        find_started = time.monotonic()
        button, validation = MediaMarktWorkerCase.fast_revalidate_buy_button(
            driver,
            target=target,
            timeline=timeline,
        )
        button_find_ms = (time.monotonic() - find_started) * 1000
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "mediamarkt_fast_button_find_finished",
            driver,
            step_name="button_find_ms",
            result="success" if button is not None else "rejected",
            duration_ms=button_find_ms,
            selector="button#pdp-add-to-cart-button,[data-test*='cofr-add-to-basket-button']",
            validation_reason=validation.get("reason"),
            element=validation.get("button_summary"),
        )
        if button is None:
            decision = validation.get("availability_decision") or {}
            if decision.get("decision") == "unavailable":
                if trace is not None and validation.get("rejection_markers"):
                    trace.set_result(
                        "mediamarkt_warm_action_stale_stock",
                        {
                            "dom_status": validation.get("reason"),
                            "markers": validation.get("rejection_markers"),
                            "availability_decision": decision,
                        },
                    )
            raise RuntimeError(f"mediamarkt_warm_fast_revalidate_failed: {validation.get('reason')}")

        visible_button_elapsed_ms = None
        if job_received_monotonic is not None:
            visible_button_elapsed_ms = (time.monotonic() - job_received_monotonic) * 1000
            MediaMarktWorkerCase._record_timeline_event(
                timeline,
                "mediamarkt_job_received_to_visible_button_timing",
                driver,
                step_name="mediamarkt_job_received_to_visible_button_ms",
                result="success",
                duration_ms=visible_button_elapsed_ms,
                mediamarkt_job_received_to_visible_button_ms=round(visible_button_elapsed_ms, 3),
                availability_decision=validation.get("availability_decision") or {},
                element=validation.get("button_summary"),
            )

        click_started = time.monotonic()
        MediaMarktWorkerCase._fast_click_buy_button(driver, button, timeline=timeline)
        click_ms = (time.monotonic() - click_started) * 1000
        click_elapsed_ms = None
        if job_received_monotonic is not None:
            click_elapsed_ms = (time.monotonic() - job_received_monotonic) * 1000
        switch_to_click_ms = None
        if warm_tab_switched_monotonic is not None:
            switch_to_click_ms = (time.monotonic() - warm_tab_switched_monotonic) * 1000
        hot_path_to_click_ms = None
        if hot_path_started_monotonic is not None:
            hot_path_to_click_ms = (time.monotonic() - hot_path_started_monotonic) * 1000
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "mediamarkt_job_received_to_click_timing",
            driver,
            step_name="mediamarkt_job_received_to_click_ms",
            result="success",
            duration_ms=click_elapsed_ms,
            click_ms=round(click_ms, 3),
            total_job_received_to_click_ms=click_elapsed_ms,
            mediamarkt_job_received_to_click_ms=round(click_elapsed_ms, 3) if click_elapsed_ms is not None else None,
            mediamarkt_hot_path_started_to_click_ms=round(hot_path_to_click_ms, 3) if hot_path_to_click_ms is not None else None,
            warm_tab_switch_to_click_ms=switch_to_click_ms,
            element=summarize_element(button),
        )
        if hot_path_to_click_ms is not None:
            MediaMarktWorkerCase._record_timeline_event(
                timeline,
                "mediamarkt_hot_path_started_to_click_timing",
                driver,
                step_name="mediamarkt_hot_path_started_to_click_ms",
                result="success",
                duration_ms=hot_path_to_click_ms,
                mediamarkt_hot_path_started_to_click_ms=round(hot_path_to_click_ms, 3),
                element=summarize_element(button),
            )
        MediaMarktWorkerCase._runtime_log_event(
            "mediamarkt_job_received_to_click_timing",
            action_id=timeline.action_id if timeline is not None else getattr(trace, "action_id", None),
            total_job_received_to_click_ms=round(click_elapsed_ms, 3) if click_elapsed_ms is not None else None,
            mediamarkt_job_received_to_visible_button_ms=(
                round(visible_button_elapsed_ms, 3) if visible_button_elapsed_ms is not None else None
            ),
            mediamarkt_job_received_to_click_ms=round(click_elapsed_ms, 3) if click_elapsed_ms is not None else None,
            mediamarkt_hot_path_started_to_click_ms=round(hot_path_to_click_ms, 3) if hot_path_to_click_ms is not None else None,
            warm_tab_switch_to_click_ms=round(switch_to_click_ms, 3) if switch_to_click_ms is not None else None,
            click_ms=round(click_ms, 3),
            element=summarize_element(button),
        )
        if trace is not None:
            trace.step("Clicked add-to-cart", {"phase": "add_to_cart", "url": driver.current_url, "action_path": "warm_tab"})
        confirmation_started = time.monotonic()
        confirmation = MediaMarktWorkerCase._wait_add_to_cart_confirmation_fast(
            driver,
            timeline=timeline,
            timeout=3.0,
        )
        MediaMarktWorkerCase._record_timeline_event(
            timeline,
            "add_to_cart_confirmation_finished",
            driver,
            step_name="add_to_cart_confirmation_ms",
            result=confirmation,
            duration_ms=(time.monotonic() - confirmation_started) * 1000,
        )
        if confirmation == "unavailable":
            if trace is not None:
                trace.set_result("out_of_stock_after_cart", {"phase": "after_add_to_cart"})
            raise RuntimeError("out_of_stock_after_cart: after_add_to_cart")
        MediaMarktWorkerCase._fast_queue_check(
            driver,
            phase="after_warm_add_to_cart",
            cfg=cfg,
            trace=trace,
            timeline=timeline,
            timeout_seconds=0.25,
        )


    @staticmethod
    def checkout(
        driver: WebDriver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
        timeline: WorkerActionTimeline | None = None,
        job_received_monotonic: float | None = None,
    ) -> None:
        timing = MediaMarktWorkerCase._apply_timing(cfg)
        checkout_started_monotonic = time.monotonic()
        if job_received_monotonic is not None:
            elapsed_ms = (checkout_started_monotonic - job_received_monotonic) * 1000
            MediaMarktWorkerCase._record_timeline_event(
                timeline,
                "mediamarkt_job_received_to_checkout_timing",
                driver,
                step_name="total_job_received_to_checkout_ms",
                result="checkout_started",
                duration_ms=elapsed_ms,
                total_job_received_to_checkout_ms=elapsed_ms,
            )
        if trace is not None:
            trace.step("Checkout started", {"phase": "checkout", "url": driver.current_url})
        MediaMarktWorkerCase._debug_page(driver, "checkout start")
        MediaMarktWorkerCase._wait_dom_settle(driver, timeout=5.0)
        wait_if_queue(driver, site="mediamarkt", phase="checkout start", cfg=cfg, trace=trace)
        MediaMarktWorkerCase._raise_if_checkout_unavailable(
            driver,
            step_name="checkout_start",
            trace=trace,
            timeline=timeline,
        )

        # Мягкий prefill: не опираемся на него как на обязательный сигнал
        if trace is not None:
            trace.step("Prefilling checkout contact fields", {"phase": "checkout_details"})
        MediaMarktWorkerCase._fill_checkout_contact_fields(driver, cfg)
        MediaMarktWorkerCase._raise_if_checkout_unavailable(
            driver,
            step_name="after_prefill",
            trace=trace,
            timeline=timeline,
        )

        step = MediaMarktWorkerCase._resolve_checkout_step_stable(driver, timeout=6)
        print(f"[mediamarkt] initial stable_step={step}")
        MediaMarktWorkerCase._raise_if_checkout_unavailable(
            driver,
            step_name=f"initial_step_{step}",
            trace=trace,
            timeline=timeline,
        )

        if step == "unknown":
            print("[mediamarkt] initial state unknown, opening checkout directly")
            if trace is not None:
                trace.warning("Unknown initial state, opening checkout directly", {"phase": "recovery"})
            driver.get(MediaMarktWorkerCase.CHECKOUT_URL)
            MediaMarktWorkerCase._wait_dom_settle(driver, timeout=5.0)
            MediaMarktWorkerCase._sleep(timing.after_navigation_wait_seconds)
            MediaMarktWorkerCase._debug_page(driver, "after direct checkout open")
            wait_if_queue(driver, site="mediamarkt", phase="after direct checkout open", cfg=cfg, trace=trace)
            MediaMarktWorkerCase._raise_if_checkout_unavailable(
                driver,
                step_name="after_direct_checkout_open",
                trace=trace,
                timeline=timeline,
            )
            MediaMarktWorkerCase._fill_checkout_contact_fields(driver, cfg)
            step = MediaMarktWorkerCase._wait_checkout_actionable(driver, timeout=18)
            print(f"[mediamarkt] after direct open actionable_step={step}")
            MediaMarktWorkerCase._raise_if_checkout_unavailable(
                driver,
                step_name=f"after_direct_open_{step}",
                trace=trace,
                timeline=timeline,
            )

        max_rounds = 6

        for round_idx in range(1, max_rounds + 1):
            MediaMarktWorkerCase._wait_dom_settle(driver, timeout=2.0)
            wait_if_queue(driver, site="mediamarkt", phase=f"checkout round {round_idx}", cfg=cfg, trace=trace)
            step = MediaMarktWorkerCase._resolve_checkout_step_stable(driver, timeout=6)
            print(f"[mediamarkt] checkout round={round_idx} resolved_step={step}")
            MediaMarktWorkerCase._raise_if_checkout_unavailable(
                driver,
                step_name=f"checkout_round_{round_idx}_{step}",
                trace=trace,
                timeline=timeline,
            )

            if step == "drawer":
                if trace is not None:
                    trace.step("Opening cart/checkout from drawer", {"phase": "open_cart"})
                MediaMarktWorkerCase._click_drawer_checkout(driver)
                step = MediaMarktWorkerCase._wait_checkout_actionable(driver, timeout=18)
                print(f"[mediamarkt] post-drawer actionable_step={step}")
                MediaMarktWorkerCase._raise_if_checkout_unavailable(
                    driver,
                    step_name=f"post_drawer_{step}",
                    trace=trace,
                    timeline=timeline,
                )
                continue

            if step == "step_ik_ga_bestellen":
                if trace is not None:
                    trace.step("Clicking Ik ga bestellen", {"phase": "checkout_step"})
                MediaMarktWorkerCase._fill_checkout_contact_fields(driver, cfg)
                wait_if_queue(driver, site="mediamarkt", phase="before Ik ga bestellen", cfg=cfg, trace=trace)
                MediaMarktWorkerCase._raise_if_checkout_unavailable(
                    driver,
                    step_name="before_ik_ga_bestellen",
                    trace=trace,
                    timeline=timeline,
                )
                MediaMarktWorkerCase._click_step_ik_ga_bestellen(driver)
                step = MediaMarktWorkerCase._wait_checkout_actionable(driver, timeout=18)
                print(f"[mediamarkt] post-ik_ga_bestellen actionable_step={step}")
                MediaMarktWorkerCase._raise_if_checkout_unavailable(
                    driver,
                    step_name=f"post_ik_ga_bestellen_{step}",
                    trace=trace,
                    timeline=timeline,
                )
                continue

            if step == "step_verder":
                if trace is not None:
                    trace.step("Clicking Verder", {"phase": "checkout_step"})
                MediaMarktWorkerCase._fill_checkout_contact_fields(driver, cfg)
                wait_if_queue(driver, site="mediamarkt", phase="before Verder", cfg=cfg, trace=trace)
                MediaMarktWorkerCase._raise_if_checkout_unavailable(
                    driver,
                    step_name="before_verder",
                    trace=trace,
                    timeline=timeline,
                )
                MediaMarktWorkerCase._click_step_verder(driver)
                step = MediaMarktWorkerCase._wait_checkout_actionable(driver, timeout=18)
                print(f"[mediamarkt] post-verder actionable_step={step}")
                MediaMarktWorkerCase._raise_if_checkout_unavailable(
                    driver,
                    step_name=f"post_verder_{step}",
                    trace=trace,
                    timeline=timeline,
                )
                continue

            if step == "step_doorgaan_en_betalen":
                if trace is not None:
                    trace.step("Clicking Doorgaan en betalen", {"phase": "checkout_step"})
                MediaMarktWorkerCase._fill_checkout_contact_fields(driver, cfg)
                wait_if_queue(driver, site="mediamarkt", phase="before Doorgaan en betalen", cfg=cfg, trace=trace)
                MediaMarktWorkerCase._raise_if_checkout_unavailable(
                    driver,
                    step_name="before_doorgaan_en_betalen",
                    trace=trace,
                    timeline=timeline,
                )
                MediaMarktWorkerCase._click_step_doorgaan_en_betalen(driver)
                step = MediaMarktWorkerCase._wait_checkout_actionable(driver, timeout=20)
                print(f"[mediamarkt] post-doorgaan_en_betalen actionable_step={step}")
                MediaMarktWorkerCase._raise_if_checkout_unavailable(
                    driver,
                    step_name=f"post_doorgaan_en_betalen_{step}",
                    trace=trace,
                    timeline=timeline,
                )
                continue

            if step == "payment":
                print("[mediamarkt] payment step reached")
                if trace is not None:
                    trace.step("Payment step reached, selecting creditcard", {"phase": "payment"})
                wait_if_queue(driver, site="mediamarkt", phase="before payment method", cfg=cfg, trace=trace)
                MediaMarktWorkerCase._try_select_creditcard(driver)
                return

            if step == "external_payment" or MediaMarktWorkerCase._is_external_payment_page(driver):
                print("[mediamarkt] external payment page reached")
                if trace is not None:
                    trace.step("External payment page reached", {"phase": "payment", "url": driver.current_url})
                MediaMarktWorkerCase._debug_page(driver, "external payment final")
                wait_if_queue(driver, site="mediamarkt", phase="before card data", cfg=cfg, trace=trace)
                MediaMarktWorkerCase._fill_card_data(driver, cfg)
                MediaMarktWorkerCase._sleep(timing.after_checkout_click_wait_seconds)
                wait_if_queue(driver, site="mediamarkt", phase="before final pay", cfg=cfg, trace=trace)
                if trace is not None:
                    trace.step("Clicking external pay button", {"phase": "final_submit"})
                MediaMarktWorkerCase._click_pay(driver)
                MediaMarktWorkerCase._wait_dom_settle(driver, timeout=5.0)
                return

            # Unknown branch: одна попытка мягкого recovery
            if step == "unknown":
                try:
                    current_url = driver.current_url.lower()
                except Exception:
                    current_url = ""

                if "checkout" not in current_url:
                    print("[mediamarkt] unknown step outside checkout, reopening checkout")
                    if trace is not None:
                        trace.warning("Unknown checkout state, reopening checkout", {"phase": "recovery"})
                    driver.get(MediaMarktWorkerCase.CHECKOUT_URL)
                    MediaMarktWorkerCase._wait_dom_settle(driver, timeout=5.0)
                    MediaMarktWorkerCase._sleep(timing.after_navigation_wait_seconds)
                    wait_if_queue(driver, site="mediamarkt", phase="after recovery checkout open", cfg=cfg, trace=trace)
                    MediaMarktWorkerCase._raise_if_checkout_unavailable(
                        driver,
                        step_name="after_recovery_checkout_open",
                        trace=trace,
                        timeline=timeline,
                    )
                    MediaMarktWorkerCase._fill_checkout_contact_fields(driver, cfg)
                    continue

                print("[mediamarkt] unknown step inside checkout, waiting one more cycle")
                MediaMarktWorkerCase._raise_if_checkout_unavailable(
                    driver,
                    step_name="unknown_checkout_wait",
                    trace=trace,
                    timeline=timeline,
                )
                MediaMarktWorkerCase._sleep(1.0)
                continue

        MediaMarktWorkerCase._checkout_debug_snapshot(driver, f"checkout_failed_{step}")
        raise RuntimeError(f"mediamarkt: checkout did not reach payment step, ended on step={step}")

    @staticmethod
    def add_to_cart_and_checkout(
        driver: WebDriver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        MediaMarktWorkerCase._apply_timing(cfg)
        MediaMarktWorkerCase.add_to_cart(driver, target, cfg, trace)
        MediaMarktWorkerCase.checkout(driver, target, cfg, trace)

        # Даем странице коротко стабилизироваться и освобождаем очередь
        MediaMarktWorkerCase._wait_dom_settle(driver, timeout=3.0)
        MediaMarktWorkerCase._sleep(1.0)
