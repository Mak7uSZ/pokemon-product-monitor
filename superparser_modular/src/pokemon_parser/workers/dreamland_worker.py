from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Callable

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from pokemon_parser.config import AppConfig
from pokemon_parser.dreamland_availability import detect_dreamland_availability_from_driver
from pokemon_parser.models import ActionTarget
from pokemon_parser.workers.base import BaseWorkerCase
from pokemon_parser.workers.queue import detect_queue_page, wait_if_queue
from pokemon_parser.workers.timing import WorkerTiming, build_worker_timing
from pokemon_parser.workers.trace import WorkerTraceLogger


class DreamLandWorkerCase(BaseWorkerCase):
    BASE_URL = "https://www.dreamland.nl"
    DEFAULT_CART_URL = f"{BASE_URL}/cart"
    DEFAULT_CHECKOUT_URL = f"{BASE_URL}/checkout"

    WAIT_TIMEOUT = 20
    STEP_TIMEOUT = 18
    POLL = 0.2

    SHORT_PAUSE = 0.2
    CLICK_RETRY_PAUSE = 0.45
    POST_CLICK_SETTLE = 0.6

    DEBUG_DIR = Path("debug_artifacts") / "dreamland"

    RESULT_UNAVAILABLE = "unavailable_at_worker_validation"
    RESULT_ADD_BUTTON_MISSING = "add_to_cart_button_missing_unavailable"
    RESULT_CART_EMPTY = "checkout_button_missing_cart_empty"

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def _apply_timing(cfg: AppConfig) -> WorkerTiming:
        timing = build_worker_timing(cfg)
        DreamLandWorkerCase.WAIT_TIMEOUT = max(5, int(round(timing.wait_timeout_seconds)))
        DreamLandWorkerCase.STEP_TIMEOUT = max(5, int(round(timing.wait_timeout_seconds)))
        DreamLandWorkerCase.POLL = timing.poll_seconds
        DreamLandWorkerCase.SHORT_PAUSE = timing.click_pause_seconds
        DreamLandWorkerCase.CLICK_RETRY_PAUSE = timing.retry_pause_seconds
        DreamLandWorkerCase.POST_CLICK_SETTLE = timing.after_checkout_click_wait_seconds
        return timing

    @staticmethod
    def _wait(driver: WebDriver, timeout: int | None = None) -> WebDriverWait:
        return WebDriverWait(
            driver,
            timeout or DreamLandWorkerCase.WAIT_TIMEOUT,
            poll_frequency=DreamLandWorkerCase.POLL,
            ignored_exceptions=(StaleElementReferenceException,),
        )

    @staticmethod
    def _product_url(target: ActionTarget) -> str:
        if not target.product_url:
            raise RuntimeError("dreamland: missing product_url")
        return target.product_url

    @staticmethod
    def _cart_url(target: ActionTarget) -> str:
        cart_url = (
            target.checkout.cart_url
            if target.checkout is not None and target.checkout.cart_url
            else DreamLandWorkerCase.DEFAULT_CART_URL
        )
        if cart_url.rstrip("/").endswith("/winkelmand"):
            return DreamLandWorkerCase.DEFAULT_CART_URL
        return cart_url

    @staticmethod
    def _checkout_url(target: ActionTarget) -> str:
        if target.checkout is not None and target.checkout.checkout_url:
            return target.checkout.checkout_url
        return DreamLandWorkerCase.DEFAULT_CHECKOUT_URL

    @staticmethod
    def _safe_current_url(driver: WebDriver) -> str:
        try:
            return driver.current_url
        except Exception:
            return ""

    @staticmethod
    def _safe_title(driver: WebDriver) -> str:
        try:
            return driver.title
        except Exception:
            return ""

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
    def _controlled_abort(
        driver: WebDriver,
        trace: WorkerTraceLogger | None,
        *,
        status: str,
        reason: str,
        phase: str,
        metadata: dict | None = None,
        snapshot_label: str | None = None,
    ) -> None:
        details = {
            "phase": phase,
            "status": status,
            "reason": reason,
            "url": DreamLandWorkerCase._safe_current_url(driver),
            "page_title": DreamLandWorkerCase._safe_title(driver),
            **(metadata or {}),
        }
        if trace is not None:
            trace.set_result(status, details)
            trace.warning(
                "Dreamland unavailable at worker validation. Action aborted safely."
                if status == DreamLandWorkerCase.RESULT_UNAVAILABLE
                else "Dreamland worker aborted before unsafe checkout click",
                details,
                level="minimal",
            )
        if snapshot_label:
            DreamLandWorkerCase._debug_snapshot(driver, snapshot_label)
            DreamLandWorkerCase._dump_debug_metadata(snapshot_label, details)
        raise RuntimeError(f"dreamland: {status} reason={reason}")

    @staticmethod
    def _validate_product_purchasable(
        driver: WebDriver,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        queue_state = detect_queue_page(driver, "dreamland")
        if queue_state.in_queue:
            if trace is not None:
                trace.step(
                    "Dreamland availability validation deferred while in queue",
                    {
                        "phase": "product_validation",
                        "url": queue_state.url,
                        "signals": list(queue_state.signals),
                    },
                    level="verbose",
                )
            return

        availability = detect_dreamland_availability_from_driver(driver)
        metadata = {
            "availability_status": availability.status,
            "availability_reason": availability.reason,
            "negative_signals": list(availability.negative_signals),
            "positive_signals": list(availability.positive_signals),
            "cta_texts": list(availability.cta_texts[:8]),
        }
        print(
            "[dreamland] worker availability "
            f"purchasable={availability.purchasable} status={availability.status} "
            f"reason={availability.reason} negative={list(availability.negative_signals)} "
            f"positive={list(availability.positive_signals)}"
        )

        if not availability.purchasable and availability.status == "unavailable":
            DreamLandWorkerCase._controlled_abort(
                driver,
                trace,
                status=DreamLandWorkerCase.RESULT_UNAVAILABLE,
                reason=availability.reason,
                phase="product_validation",
                metadata=metadata,
                snapshot_label="unavailable_at_worker_validation",
            )

        if not availability.purchasable:
            DreamLandWorkerCase._controlled_abort(
                driver,
                trace,
                status=DreamLandWorkerCase.RESULT_ADD_BUTTON_MISSING,
                reason=availability.reason,
                phase="product_validation",
                metadata=metadata,
                snapshot_label="add_to_cart_button_missing_unavailable",
            )

        if trace is not None:
            trace.step(
                "Dreamland product purchasable validation passed",
                {
                    "phase": "product_validation",
                    "url": DreamLandWorkerCase._safe_current_url(driver),
                    **metadata,
                },
                level="verbose",
            )

    @staticmethod
    def _debug_page(driver: WebDriver, prefix: str) -> None:
        try:
            print(f"[dreamland] {prefix} current_url={driver.current_url}")
        except Exception:
            print(f"[dreamland] {prefix} current_url=<unavailable>")

        try:
            print(f"[dreamland] {prefix} title={driver.title}")
        except Exception:
            print(f"[dreamland] {prefix} title=<unavailable>")

        try:
            print(f"[dreamland] {prefix} cookies={len(driver.get_cookies())}")
        except Exception:
            print(f"[dreamland] {prefix} cookies=<unavailable>")

    @staticmethod
    def _ensure_debug_dir() -> None:
        os.makedirs(DreamLandWorkerCase.DEBUG_DIR, exist_ok=True)

    @staticmethod
    def _dump_debug_artifacts(driver: WebDriver, label: str) -> None:
        DreamLandWorkerCase._ensure_debug_dir()
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_label = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in label)

        png_path = DreamLandWorkerCase.DEBUG_DIR / f"{ts}_{safe_label}.png"
        html_path = DreamLandWorkerCase.DEBUG_DIR / f"{ts}_{safe_label}.html"

        try:
            driver.save_screenshot(str(png_path))
            print(f"[dreamland] saved screenshot={png_path}")
        except Exception as exc:
            print(f"[dreamland] failed saving screenshot error={type(exc).__name__}: {exc}")

        try:
            html_path.write_text(driver.page_source, encoding="utf-8")
            print(f"[dreamland] saved html={html_path}")
        except Exception as exc:
            print(f"[dreamland] failed saving html error={type(exc).__name__}: {exc}")

    @staticmethod
    def _dump_debug_metadata(label: str, details: dict) -> None:
        DreamLandWorkerCase._ensure_debug_dir()
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_label = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in label)
        json_path = DreamLandWorkerCase.DEBUG_DIR / f"{ts}_{safe_label}.json"
        try:
            json_path.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[dreamland] saved metadata={json_path}")
        except Exception as exc:
            print(f"[dreamland] failed saving metadata error={type(exc).__name__}: {exc}")

    @staticmethod
    def _debug_snapshot(driver: WebDriver, label: str) -> None:
        DreamLandWorkerCase._debug_page(driver, label)
        DreamLandWorkerCase._dump_debug_artifacts(driver, label)

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
                DreamLandWorkerCase._sleep(0.15)
                continue

            if signature == last_signature and ready in ("interactive", "complete"):
                stable_hits += 1
                if stable_hits >= 3:
                    return
            else:
                stable_hits = 0

            last_signature = signature
            DreamLandWorkerCase._sleep(0.15)

    @staticmethod
    def _is_visible(el: WebElement) -> bool:
        try:
            return el.is_displayed()
        except Exception:
            return False

    @staticmethod
    def _is_enabled(el: WebElement) -> bool:
        try:
            return el.is_enabled()
        except Exception:
            return False

    @staticmethod
    def _has_real_box(el: WebElement) -> bool:
        try:
            rect = el.rect or {}
            return (rect.get("width", 0) or 0) > 0 and (rect.get("height", 0) or 0) > 0
        except Exception:
            return False

    @staticmethod
    def _is_clickable_candidate(el: WebElement) -> bool:
        return (
            DreamLandWorkerCase._is_visible(el)
            and DreamLandWorkerCase._is_enabled(el)
            and DreamLandWorkerCase._has_real_box(el)
        )

    @staticmethod
    def _find_visible_elements(driver: WebDriver, locator: tuple[str, str]) -> list[WebElement]:
        try:
            elements = driver.find_elements(*locator)
        except Exception:
            return []
        return [el for el in elements if DreamLandWorkerCase._is_visible(el)]

    @staticmethod
    def _find_clickable_element(driver: WebDriver, locator: tuple[str, str]) -> WebElement | bool:
        visible = DreamLandWorkerCase._find_visible_elements(driver, locator)
        clickable = [el for el in visible if DreamLandWorkerCase._is_clickable_candidate(el)]
        return clickable[0] if clickable else False

    @staticmethod
    def _has_visible(driver: WebDriver, locator: tuple[str, str]) -> bool:
        return len(DreamLandWorkerCase._find_visible_elements(driver, locator)) > 0

    @staticmethod
    def _scroll_into_view(driver: WebDriver, el: WebElement) -> None:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            el,
        )
        DreamLandWorkerCase._sleep(DreamLandWorkerCase.SHORT_PAUSE)

    @staticmethod
    def _click_element(driver: WebDriver, el: WebElement) -> None:
        DreamLandWorkerCase._scroll_into_view(driver, el)
        try:
            el.click()
        except (ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException):
            driver.execute_script("arguments[0].click();", el)

    @staticmethod
    def _xpath_literal(value: str) -> str:
        if "'" not in value:
            return f"'{value}'"
        if '"' not in value:
            return f'"{value}"'

        parts = value.split("'")
        segments: list[str] = []
        for idx, part in enumerate(parts):
            if part:
                segments.append(f"'{part}'")
            if idx != len(parts) - 1:
                segments.append("\"'\"")
        return f"concat({', '.join(segments)})"

    @staticmethod
    def _exact_text_xpath(text: str) -> str:
        literal = DreamLandWorkerCase._xpath_literal(text)
        return (
            f"//button[normalize-space()={literal}]"
            f"|//button[.//span[normalize-space()={literal}]]"
            f"|//a[normalize-space()={literal}]"
            f"|//a[.//span[normalize-space()={literal}]]"
            f"|//label[normalize-space()={literal}]"
            f"|//label[.//span[normalize-space()={literal}]]"
        )

    @staticmethod
    def _contains_text_xpath(text: str) -> str:
        literal = DreamLandWorkerCase._xpath_literal(text)
        return (
            f"//button[contains(normalize-space(.), {literal})]"
            f"|//button[.//span[contains(normalize-space(.), {literal})]]"
            f"|//a[contains(normalize-space(.), {literal})]"
            f"|//a[.//span[contains(normalize-space(.), {literal})]]"
            f"|//label[contains(normalize-space(.), {literal})]"
            f"|//label[.//span[contains(normalize-space(.), {literal})]]"
        )

    @staticmethod
    def _try_click_exact_text(driver: WebDriver, text: str, label: str, attempts: int = 4) -> bool:
        xpath = DreamLandWorkerCase._exact_text_xpath(text)
        last_exc = None

        for attempt in range(1, attempts + 1):
            try:
                print(f"[dreamland] trying {label} by exact text={text!r} attempt={attempt}")
                el = DreamLandWorkerCase._wait(driver, 8).until(
                    lambda d: DreamLandWorkerCase._find_clickable_element(d, (By.XPATH, xpath))
                )
                DreamLandWorkerCase._click_element(driver, el)
                print(f"[dreamland] success {label} by exact text={text!r} attempt={attempt}")
                return True
            except Exception as exc:
                last_exc = exc
                print(
                    f"[dreamland] failed {label} by exact text={text!r} "
                    f"attempt={attempt} error={type(exc).__name__}: {exc}"
                )
                DreamLandWorkerCase._sleep(DreamLandWorkerCase.CLICK_RETRY_PAUSE)

        print(f"[dreamland] exact text click failed label={label} text={text!r} last_exc={last_exc}")
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
                    print(f"[dreamland] trying {label} locator={locator} attempt={attempt}")
                    el = DreamLandWorkerCase._wait(driver, timeout).until(
                        lambda d: DreamLandWorkerCase._find_clickable_element(d, locator)
                    )
                    DreamLandWorkerCase._click_element(driver, el)
                    print(f"[dreamland] success {label} locator={locator} attempt={attempt}")
                    return True
                except Exception as exc:
                    last_exc = exc
                    print(
                        f"[dreamland] failed {label} locator={locator} "
                        f"attempt={attempt} error={type(exc).__name__}: {exc}"
                    )
                    DreamLandWorkerCase._sleep(DreamLandWorkerCase.CLICK_RETRY_PAUSE)

        print(f"[dreamland] locator click failed label={label} last_exc={last_exc}")
        return False

    @staticmethod
    def _try_click_js_query(driver: WebDriver, selectors: list[str], label: str) -> bool:
        for selector in selectors:
            try:
                print(f"[dreamland] trying js {label} selector={selector}")
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
                    print(f"[dreamland] success js {label} selector={selector}")
                    return True
            except Exception as exc:
                print(
                    f"[dreamland] failed js {label} selector={selector} "
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
            except Exception:
                return False

        print(f"[dreamland] waiting for condition label={label}")
        DreamLandWorkerCase._wait(driver, timeout).until(_wrapped)
        DreamLandWorkerCase._sleep(DreamLandWorkerCase.POST_CLICK_SETTLE)
        DreamLandWorkerCase._wait_dom_settle(driver, timeout=4.0)
        DreamLandWorkerCase._debug_page(driver, f"after {label}")

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
        exact_attempts: int = 4,
        fallback_attempts: int = 2,
        locator_timeout: int = 6,
    ) -> None:
        clicked = DreamLandWorkerCase._try_click_exact_text(driver, text, label=label, attempts=exact_attempts)

        if not clicked and fallback_locators:
            clicked = DreamLandWorkerCase._try_click_locators(
                driver,
                fallback_locators,
                label=label,
                attempts=fallback_attempts,
                timeout=locator_timeout,
            )

        if not clicked and fallback_js_selectors:
            clicked = DreamLandWorkerCase._try_click_js_query(
                driver,
                fallback_js_selectors,
                label=label,
            )

        if not clicked:
            raise RuntimeError(f"dreamland: step button not clicked label={label} text={text!r}")

        DreamLandWorkerCase._wait_for_condition(driver, label, confirm, timeout=timeout)

    @staticmethod
    def _detect_checkout_step(driver: WebDriver) -> str:
        page_text = DreamLandWorkerCase._page_text(driver)
        current_url = DreamLandWorkerCase._safe_current_url(driver).lower()

        if DreamLandWorkerCase._is_login_page(driver):
            return "login"

        if (
            DreamLandWorkerCase._has_visible(driver, (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Naar winkelmandje")))
            or "toegevoegd aan je winkelwagen" in page_text
        ):
            return "modal"

        if (
            DreamLandWorkerCase._has_visible(driver, (By.CSS_SELECTOR, "button[data-cart-continue]"))
            or "verder naar bestellen" in page_text
            or "/cart" in current_url
        ):
            return "cart"

        if (
            DreamLandWorkerCase._has_visible(driver, (By.CSS_SELECTOR, "input#summary_termsAndConditionsAccepted"))
            or "ik ga akkoord met de algemene voorwaarden" in page_text
            or "afrekenen met creditcard" in page_text
        ):
            return "summary"

        if (
            DreamLandWorkerCase._has_visible(driver, (By.CSS_SELECTOR, "button#details_order_submit"))
            or "doorgaan naar betaalwijze" in page_text
            or "levering op een ander adres" in page_text
        ):
            return "details"

        if (
            DreamLandWorkerCase._has_visible(driver, (By.CSS_SELECTOR, 'iframe[title="cardNumber input"]'))
            or DreamLandWorkerCase._has_visible(driver, (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Naar besteloverzicht")))
            or "creditcard" in page_text
        ):
            return "payment"

        if "/checkout" in current_url:
            return "checkout_unknown"

        return "unknown"

    @staticmethod
    def _cart_empty_signals(driver: WebDriver) -> list[str]:
        page_text = DreamLandWorkerCase._page_text(driver)
        signals = []
        for marker in (
            "winkelmandje is leeg",
            "je winkelmandje is leeg",
            "cart is empty",
            "geen artikelen",
            "geen producten",
            "nog geen artikelen",
        ):
            if marker in page_text:
                signals.append(marker)
        return signals

    @staticmethod
    def _cart_contains_target(driver: WebDriver, target: ActionTarget) -> bool:
        page_text = DreamLandWorkerCase._page_text(driver)
        page_norm = " ".join(page_text.split())
        title_tokens = [token for token in target.title.lower().split() if len(token) >= 4]
        important_tokens = title_tokens[:5]
        if important_tokens and all(token in page_norm for token in important_tokens):
            return True
        if target.external_id and str(target.external_id).lower() in page_text:
            return True
        return False

    @staticmethod
    def _checkout_button_visible(driver: WebDriver) -> bool:
        locators = [
            (By.CSS_SELECTOR, "button[data-cart-continue]"),
            (By.CSS_SELECTOR, "button.cart-overview__next"),
            (By.CSS_SELECTOR, 'button[form="cart-items-form"][value="next-step"]'),
            (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Verder naar bestellen")),
        ]
        return any(DreamLandWorkerCase._has_visible(driver, locator) for locator in locators)

    @staticmethod
    def _validate_cart_before_checkout(
        driver: WebDriver,
        target: ActionTarget,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        empty_signals = DreamLandWorkerCase._cart_empty_signals(driver)
        contains_target = DreamLandWorkerCase._cart_contains_target(driver, target)
        checkout_visible = DreamLandWorkerCase._checkout_button_visible(driver)
        metadata = {
            "cart_empty_signals": empty_signals,
            "cart_contains_target": contains_target,
            "checkout_button_visible": checkout_visible,
            "selector": "button[data-cart-continue] / Verder naar bestellen",
        }
        print(
            "[dreamland] cart validation "
            f"empty={empty_signals} contains_target={contains_target} checkout_visible={checkout_visible} "
            f"url={DreamLandWorkerCase._safe_current_url(driver)}"
        )

        if empty_signals:
            DreamLandWorkerCase._controlled_abort(
                driver,
                trace,
                status=DreamLandWorkerCase.RESULT_CART_EMPTY,
                reason="cart_empty_before_checkout",
                phase="cart_validation",
                metadata=metadata,
                snapshot_label="cart_empty_before_checkout",
            )

        if not checkout_visible:
            DreamLandWorkerCase._controlled_abort(
                driver,
                trace,
                status=DreamLandWorkerCase.RESULT_CART_EMPTY,
                reason="checkout_button_missing_cart_not_actionable",
                phase="cart_validation",
                metadata=metadata,
                snapshot_label="checkout_button_missing_cart_not_actionable",
            )

        if trace is not None:
            trace.step(
                "Dreamland cart validation passed",
                {
                    "phase": "cart_validation",
                    "url": DreamLandWorkerCase._safe_current_url(driver),
                    **metadata,
                },
                level="verbose",
            )

    @staticmethod
    def _resolve_checkout_step_stable(
        driver: WebDriver,
        timeout: int = 8,
        stable_hits_required: int = 2,
    ) -> str:
        print("[dreamland] resolving checkout step (stable)...")
        end = time.time() + timeout
        last_step = None
        stable_hits = 0

        while time.time() < end:
            DreamLandWorkerCase._wait_dom_settle(driver, timeout=1.2)
            step = DreamLandWorkerCase._detect_checkout_step(driver)

            if step not in {"unknown", "checkout_unknown"} and step == last_step:
                stable_hits += 1
                if stable_hits >= stable_hits_required:
                    print(f"[dreamland] stable checkout step resolved={step}")
                    return step
            else:
                stable_hits = 0

            last_step = step
            DreamLandWorkerCase._sleep(0.3)

        final_step = DreamLandWorkerCase._detect_checkout_step(driver)
        print(f"[dreamland] stable checkout step timeout final_step={final_step}")
        return final_step

    @staticmethod
    def _wait_checkout_actionable(driver: WebDriver, timeout: int = 18) -> str:
        print("[dreamland] waiting for checkout to become actionable...")

        def _probe(d: WebDriver):
            step = DreamLandWorkerCase._detect_checkout_step(d)
            if step not in {"unknown", "checkout_unknown"}:
                return step
            return False

        try:
            DreamLandWorkerCase._wait(driver, timeout).until(_probe)
        except Exception:
            pass

        DreamLandWorkerCase._sleep(DreamLandWorkerCase.POST_CLICK_SETTLE)
        DreamLandWorkerCase._wait_dom_settle(driver, timeout=3.0)
        return DreamLandWorkerCase._resolve_checkout_step_stable(driver, timeout=6)

    @staticmethod
    def _is_login_page(driver: WebDriver) -> bool:
        current_url = DreamLandWorkerCase._safe_current_url(driver).lower()
        page_text = DreamLandWorkerCase._page_text(driver)

        has_login_button = DreamLandWorkerCase._has_visible(
            driver,
            (By.CSS_SELECTOR, "form[data-login-form] button#submit, form[data-login-form] button[data-login-button]"),
        )
        has_password = DreamLandWorkerCase._has_visible(
            driver,
            (By.CSS_SELECTOR, 'form[data-login-form] input[type="password"]'),
        )

        return (
            "/login" in current_url
            or "/inloggen" in current_url
            or (
                has_login_button
                and has_password
                and ("al klant bij dreamland" in page_text or "wachtwoord" in page_text)
            )
        )

    @staticmethod
    def _login_form_values(driver: WebDriver) -> tuple[str, str]:
        try:
            values = driver.execute_script(
                """
                const username = document.querySelector('form[data-login-form] input#_username');
                const password = document.querySelector('form[data-login-form] input#_password');
                return [
                    username ? (username.value || '') : '',
                    password ? (password.value || '') : '',
                ];
                """
            )
            if isinstance(values, (list, tuple)) and len(values) == 2:
                return str(values[0] or ""), str(values[1] or "")
        except Exception:
            pass
        return "", ""

    @staticmethod
    def _wait_login_form_ready(driver: WebDriver, timeout: int = 8) -> tuple[str, str]:
        def _probe(d: WebDriver):
            username, password = DreamLandWorkerCase._login_form_values(d)
            if username.strip() and password.strip():
                return username, password
            return False

        return DreamLandWorkerCase._wait(driver, timeout).until(_probe)

    @staticmethod
    def _click_login_submit_once(driver: WebDriver, round_idx: int) -> None:
        label = f"login_submit_round_{round_idx}"

        clicked = DreamLandWorkerCase._try_click_locators(
            driver,
            [
                (By.CSS_SELECTOR, "form[data-login-form] button#submit[data-login-button]"),
                (By.CSS_SELECTOR, "form[data-login-form] button#submit"),
                (By.CSS_SELECTOR, "form[data-login-form] button[data-login-button]"),
                (By.CSS_SELECTOR, "form[data-login-form] button[type='submit']"),
            ],
            label=label,
            attempts=3,
            timeout=8,
        )

        if not clicked:
            try:
                clicked = bool(
                    driver.execute_script(
                        """
                        const form = document.querySelector('form[data-login-form]');
                        const button =
                            document.querySelector('form[data-login-form] button#submit[data-login-button]') ||
                            document.querySelector('form[data-login-form] button#submit') ||
                            document.querySelector('form[data-login-form] button[data-login-button]') ||
                            document.querySelector('form[data-login-form] button[type="submit"]');
                        if (!form || !button) return false;
                        button.scrollIntoView({block: 'center', inline: 'center'});
                        if (typeof form.requestSubmit === 'function') {
                            form.requestSubmit(button);
                        } else {
                            button.click();
                        }
                        return true;
                        """
                    )
                )
            except Exception:
                clicked = False

        if not clicked:
            raise RuntimeError("dreamland: login submit button not clicked")

    @staticmethod
    def _click_login_submit_if_needed(driver: WebDriver) -> bool:
        if not DreamLandWorkerCase._is_login_page(driver):
            return False

        max_login_rounds = 3

        for round_idx in range(1, max_login_rounds + 1):
            DreamLandWorkerCase._debug_page(driver, f"login page detected round={round_idx}")
            before_url = DreamLandWorkerCase._safe_current_url(driver).lower()

            try:
                username, _password = DreamLandWorkerCase._wait_login_form_ready(driver, timeout=8)
                print(
                    "[dreamland] login form ready "
                    f"round={round_idx} username_len={len(username.strip())} password_len=>0"
                )
            except Exception:
                print(f"[dreamland] login form autofill not detected round={round_idx}, trying submit anyway")

            DreamLandWorkerCase._click_login_submit_once(driver, round_idx)

            try:
                DreamLandWorkerCase._wait_for_condition(
                    driver,
                    f"login_submit_round_{round_idx}",
                    lambda d: (
                        DreamLandWorkerCase._safe_current_url(d).lower() != before_url
                        or not DreamLandWorkerCase._is_login_page(d)
                        or DreamLandWorkerCase._has_visible(d, (By.CSS_SELECTOR, "button#details_order_submit"))
                        or DreamLandWorkerCase._has_visible(d, (By.CSS_SELECTOR, 'iframe[title="cardNumber input"]'))
                        or DreamLandWorkerCase._has_visible(d, (By.CSS_SELECTOR, "input#summary_termsAndConditionsAccepted"))
                        or "doorgaan naar betaalwijze" in DreamLandWorkerCase._page_text(d)
                        or "naar besteloverzicht" in DreamLandWorkerCase._page_text(d)
                        or "afrekenen met creditcard" in DreamLandWorkerCase._page_text(d)
                    ),
                    timeout=6,
                )
            except Exception:
                print(f"[dreamland] login round={round_idx} confirm timeout, re-checking page state")

            DreamLandWorkerCase._sleep(0.8)
            DreamLandWorkerCase._wait_dom_settle(driver, timeout=2.5)

            if not DreamLandWorkerCase._is_login_page(driver):
                print(f"[dreamland] login flow completed after round={round_idx}")
                return True

            print(f"[dreamland] login page still present after round={round_idx}, retrying")

        raise RuntimeError("dreamland: login page still present after repeated submit attempts")

    @staticmethod
    def _fast_fill_first(driver: WebDriver, selectors: list[str], value: str, label: str) -> bool:
        if not value:
            return False

        last_exc = None

        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception as exc:
                last_exc = exc
                continue

            for el in elements:
                if not DreamLandWorkerCase._is_visible(el):
                    continue

                try:
                    driver.execute_script(
                        """
                        const el = arguments[0];
                        const value = arguments[1];
                        const tag = (el.tagName || '').toUpperCase();

                        el.scrollIntoView({block: 'center', inline: 'center'});
                        el.focus();

                        const proto = tag === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');

                        if (descriptor && descriptor.set) {
                            descriptor.set.call(el, value);
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
                    print(f"[dreamland] fast_fill success label={label} selector={selector}")
                    return True
                except Exception as exc:
                    last_exc = exc

        print(f"[dreamland] fast_fill skipped label={label} last_exc={last_exc}")
        return False

    @staticmethod
    def _fill_checkout_contact_fields(driver: WebDriver, cfg: AppConfig) -> None:
        DreamLandWorkerCase._fast_fill_first(
            driver,
            [
                "input#email",
                'input[type="email"]',
                'input[name="email"]',
                'input[name*="email"]',
                'input[id*="email"]',
            ],
            getattr(cfg, "checkout_email", ""),
            "email",
        )
        DreamLandWorkerCase._fast_fill_first(
            driver,
            [
                'input[name="firstName"]',
                'input[name*="first"]',
                'input[id*="first"]',
            ],
            getattr(cfg, "checkout_first_name", ""),
            "first_name",
        )
        DreamLandWorkerCase._fast_fill_first(
            driver,
            [
                'input[name="lastName"]',
                'input[name*="last"]',
                'input[id*="last"]',
            ],
            getattr(cfg, "checkout_last_name", ""),
            "last_name",
        )
        DreamLandWorkerCase._fast_fill_first(
            driver,
            [
                'input[name="streetName"]',
                'input[name*="street"]',
                'input[id*="street"]',
                'input[name*="address"]',
                'input[id*="address"]',
            ],
            getattr(cfg, "checkout_street", ""),
            "street",
        )
        DreamLandWorkerCase._fast_fill_first(
            driver,
            [
                'input[name="streetNumber"]',
                'input[name*="house"]',
                'input[id*="house"]',
                'input[name*="number"]',
                'input[id*="number"]',
            ],
            getattr(cfg, "checkout_house_number", ""),
            "house_number",
        )
        DreamLandWorkerCase._fast_fill_first(
            driver,
            [
                'input[name="postalCode"]',
                'input[name*="postal"]',
                'input[id*="postal"]',
                'input[name*="zip"]',
                'input[id*="zip"]',
            ],
            getattr(cfg, "checkout_zip_code", ""),
            "zip_code",
        )
        DreamLandWorkerCase._fast_fill_first(
            driver,
            [
                'input[name="city"]',
                'input[name*="city"]',
                'input[id*="city"]',
            ],
            getattr(cfg, "checkout_city", ""),
            "city",
        )

    @staticmethod
    def _set_checkbox_state(driver: WebDriver, selector: str, checked: bool) -> bool:
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
        except Exception:
            return False

        try:
            driver.execute_script(
                """
                const el = arguments[0];
                const checked = arguments[1];
                el.scrollIntoView({block: 'center', inline: 'center'});
                el.checked = checked;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                el,
                checked,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _ensure_same_delivery_address(driver: WebDriver) -> None:
        page_text = DreamLandWorkerCase._page_text(driver)
        if "levering op een ander adres" not in page_text:
            return

        DreamLandWorkerCase._try_click_exact_text(
            driver,
            "Nee",
            label="same_delivery_address",
            attempts=2,
        )

    @staticmethod
    def _ensure_promotions_opt_out(driver: WebDriver) -> None:
        page_text = DreamLandWorkerCase._page_text(driver)
        if "hou me op de hoogte van acties en promoties" not in page_text:
            return

        try:
            checkbox = driver.find_element(By.XPATH, "//label[contains(normalize-space(.), 'Hou me op de hoogte')]/preceding::input[@type='checkbox'][1]")
        except Exception:
            return

        try:
            if checkbox.is_selected():
                DreamLandWorkerCase._click_element(driver, checkbox)
        except Exception:
            pass

    @staticmethod
    def _creditcard_selected(driver: WebDriver) -> bool:
        return DreamLandWorkerCase._has_visible(driver, (By.CSS_SELECTOR, 'iframe[title="cardNumber input"]'))

    @staticmethod
    def _default_card_holder(cfg: AppConfig) -> str:
        explicit = getattr(cfg, "checkout_card_name", "").strip()
        if explicit:
            return explicit

        parts = [
            getattr(cfg, "checkout_first_name", "").strip(),
            getattr(cfg, "checkout_last_name", "").strip(),
        ]
        return " ".join(part for part in parts if part)

    @staticmethod
    def _normalize_card_number(value: str) -> str:
        return "".join(ch for ch in value if ch.isdigit())

    @staticmethod
    def _normalize_card_expiry(value: str) -> str:
        raw = (value or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())

        if len(digits) == 4:
            return f"{digits[:2]}/{digits[2:]}"
        if len(digits) == 6:
            return f"{digits[:2]}/{digits[-2:]}"
        return raw

    @staticmethod
    def _normalize_card_cvv(value: str) -> str:
        return "".join(ch for ch in value if ch.isdigit())

    @staticmethod
    def _fill_mollie_frame(driver: WebDriver, frame_locator: tuple[str, str], value: str, label: str) -> None:
        if not value:
            print(f"[dreamland] skip mollie field label={label} reason=empty_value")
            return

        def _locate_frame(d: WebDriver):
            clickable = DreamLandWorkerCase._find_clickable_element(d, frame_locator)
            if clickable:
                return clickable

            visible = DreamLandWorkerCase._find_visible_elements(d, frame_locator)
            if visible:
                return visible[0]

            return False

        frame = DreamLandWorkerCase._wait(driver, 15).until(_locate_frame)

        driver.switch_to.frame(frame)
        try:
            field = DreamLandWorkerCase._wait(driver, 10).until(
                lambda d: (
                    d.find_elements(By.CSS_SELECTOR, "input:not([type='hidden'])")
                    or d.find_elements(By.CSS_SELECTOR, "textarea")
                    or d.find_elements(By.CSS_SELECTOR, "[contenteditable='true']")
                    or d.find_elements(By.TAG_NAME, "body")
                )[0]
            )

            DreamLandWorkerCase._scroll_into_view(driver, field)
            field.click()

            try:
                field.send_keys(Keys.CONTROL, "a")
                field.send_keys(Keys.DELETE)
            except Exception:
                pass

            target = field
            try:
                active = driver.switch_to.active_element
                if active is not None and getattr(active, "tag_name", "").lower() not in {"html", "body"}:
                    target = active
            except Exception:
                pass

            target.send_keys(value)
            print(f"[dreamland] mollie fill success label={label}")
        finally:
            driver.switch_to.default_content()
            DreamLandWorkerCase._sleep(0.25)

    @staticmethod
    def _fill_card_data(driver: WebDriver, cfg: AppConfig) -> None:
        print("[dreamland] waiting for mollie card fields...")
        DreamLandWorkerCase._wait(driver, 15).until(
            lambda d: DreamLandWorkerCase._has_visible(d, (By.CSS_SELECTOR, 'iframe[title="cardNumber input"]'))
        )

        DreamLandWorkerCase._fill_mollie_frame(
            driver,
            (By.CSS_SELECTOR, 'iframe[title="cardNumber input"], iframe[name="cardNumber-input"]'),
            DreamLandWorkerCase._normalize_card_number(getattr(cfg, "checkout_card_number", "")),
            "card_number",
        )
        DreamLandWorkerCase._fill_mollie_frame(
            driver,
            (By.CSS_SELECTOR, 'iframe[title="cardHolder input"], iframe[name="cardHolder-input"]'),
            DreamLandWorkerCase._default_card_holder(cfg),
            "card_holder",
        )
        DreamLandWorkerCase._fill_mollie_frame(
            driver,
            (By.CSS_SELECTOR, 'iframe[title="expiryDate input"], iframe[name="expiryDate-input"]'),
            DreamLandWorkerCase._normalize_card_expiry(getattr(cfg, "checkout_card_expiry", "")),
            "expiry",
        )
        DreamLandWorkerCase._fill_mollie_frame(
            driver,
            (By.CSS_SELECTOR, 'iframe[title="verificationCode input"], iframe[name="verificationCode-input"]'),
            DreamLandWorkerCase._normalize_card_cvv(getattr(cfg, "checkout_card_cvv", "")),
            "cvc",
        )

    @staticmethod
    def _click_add_to_cart(
        driver: WebDriver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        DreamLandWorkerCase._debug_page(driver, "before add_to_cart")
        wait_if_queue(driver, site="dreamland", phase="before add_to_cart", cfg=cfg, trace=trace)
        DreamLandWorkerCase._validate_product_purchasable(driver, trace)
        if trace is not None:
            trace.step("Searching add-to-cart button", {"phase": "add_to_cart"})

        fallback_locators = [
            (By.CSS_SELECTOR, 'form[action*="/cart/add"] button[data-submit-button]'),
            (By.CSS_SELECTOR, 'form[action*="/cart/add"] button'),
            (By.CSS_SELECTOR, 'form[data-component="cart/add-to-cart-action-form"] button'),
            (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Levering aan huis")),
            (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Bestel nu")),
        ]

        if target.add_to_cart is not None and target.add_to_cart.pdp_button_selector:
            fallback_locators.append((By.CSS_SELECTOR, target.add_to_cart.pdp_button_selector))

        clicked = DreamLandWorkerCase._try_click_exact_text(
            driver,
            "Levering aan huis",
            "add_to_cart",
            attempts=4,
        )

        if not clicked:
            clicked = DreamLandWorkerCase._try_click_exact_text(
                driver,
                "Bestel nu",
                "add_to_cart",
                attempts=2,
            )

        if not clicked:
            clicked = DreamLandWorkerCase._try_click_locators(
                driver,
                fallback_locators,
                label="add_to_cart",
                attempts=3,
                timeout=8,
            )

        if not clicked:
            clicked = DreamLandWorkerCase._try_click_js_query(
                driver,
                [
                    'form[action*="/cart/add"] button[data-submit-button]',
                    'form[action*="/cart/add"] button',
                    'form[data-component="cart/add-to-cart-action-form"] button',
                ],
                label="add_to_cart",
            )

        if not clicked:
            try:
                clicked = bool(
                    driver.execute_script(
                        """
                        const form = document.querySelector('form[action*="/cart/add"]');
                        if (!form) return false;
                        form.scrollIntoView({block: 'center', inline: 'center'});
                        if (typeof form.requestSubmit === 'function') {
                            form.requestSubmit();
                        } else {
                            form.submit();
                        }
                        return true;
                        """
                    )
                )
            except Exception:
                clicked = False

        if not clicked:
            DreamLandWorkerCase._controlled_abort(
                driver,
                trace,
                status=DreamLandWorkerCase.RESULT_ADD_BUTTON_MISSING,
                reason="add_to_cart_button_not_clicked",
                phase="add_to_cart",
                metadata={
                    "selector": "Levering aan huis / Bestel nu / cart add form",
                },
                snapshot_label="add_to_cart_button_not_clicked",
            )

    @staticmethod
    def _click_modal_to_cart(driver: WebDriver) -> None:
        DreamLandWorkerCase._click_with_confirm(
            driver,
            text="Naar winkelmandje",
            label="open_cart_from_modal",
            confirm=lambda d: (
                "/cart" in DreamLandWorkerCase._safe_current_url(d).lower()
                or DreamLandWorkerCase._detect_checkout_step(d) in {"cart", "details", "payment", "summary"}
            ),
            fallback_locators=[
                (By.CSS_SELECTOR, 'a[href="/cart"]'),
                (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Naar winkelmandje")),
            ],
            fallback_js_selectors=[
                'a[href="/cart"]',
            ],
            timeout=15,
        )

    @staticmethod
    def _click_cart_continue(driver: WebDriver) -> None:
        DreamLandWorkerCase._click_with_confirm(
            driver,
            text="Verder naar bestellen",
            label="cart_continue",
            confirm=lambda d: DreamLandWorkerCase._detect_checkout_step(d) in {"login", "details", "payment", "summary"},
            fallback_locators=[
                (By.CSS_SELECTOR, "button[data-cart-continue]"),
                (By.CSS_SELECTOR, "button.cart-overview__next"),
                (By.CSS_SELECTOR, 'button[form="cart-items-form"][value="next-step"]'),
                (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Verder naar bestellen")),
            ],
            fallback_js_selectors=[
                "button[data-cart-continue]",
                "button.cart-overview__next",
                'button[form="cart-items-form"][value="next-step"]',
            ],
            timeout=8,
            exact_attempts=1,
            fallback_attempts=1,
            locator_timeout=4,
        )

    @staticmethod
    def _click_continue_to_payment(driver: WebDriver) -> None:
        DreamLandWorkerCase._click_with_confirm(
            driver,
            text="Doorgaan naar betaalwijze",
            label="details_continue",
            confirm=lambda d: DreamLandWorkerCase._detect_checkout_step(d) in {"payment", "summary"},
            fallback_locators=[
                (By.CSS_SELECTOR, "button#details_order_submit"),
                (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Doorgaan naar betaalwijze")),
            ],
            fallback_js_selectors=[
                "button#details_order_submit",
            ],
            timeout=20,
        )

    @staticmethod
    def _select_creditcard(driver: WebDriver) -> None:
        if DreamLandWorkerCase._creditcard_selected(driver):
            print("[dreamland] creditcard already selected")
            return

        clicked = DreamLandWorkerCase._try_click_exact_text(
            driver,
            "Creditcard",
            label="select_creditcard",
            attempts=3,
        )

        if not clicked:
            clicked = DreamLandWorkerCase._try_click_locators(
                driver,
                [
                    (
                        By.XPATH,
                        "//label[contains(@class, 'checkout-payment-option__label')][.//h3[normalize-space()='Creditcard']]",
                    ),
                    (
                        By.XPATH,
                        "//h3[normalize-space()='Creditcard']/ancestor::label[1]",
                    ),
                ],
                label="select_creditcard",
                attempts=2,
                timeout=6,
            )

        if not clicked:
            try:
                clicked = bool(
                    driver.execute_script(
                        """
                        const labels = [...document.querySelectorAll('label.checkout-payment-option__label')];
                        const target = labels.find((label) =>
                            (label.innerText || '').toLowerCase().includes('creditcard')
                        );
                        if (!target) return false;
                        target.scrollIntoView({block: 'center', inline: 'center'});
                        target.click();
                        return true;
                        """
                    )
                )
            except Exception:
                clicked = False

        if not clicked:
            raise RuntimeError("dreamland: creditcard option not selected")

        DreamLandWorkerCase._wait_for_condition(
            driver,
            "creditcard_selected",
            lambda d: DreamLandWorkerCase._creditcard_selected(d)
            or DreamLandWorkerCase._has_visible(d, (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Naar besteloverzicht"))),
            timeout=15,
        )

    @staticmethod
    def _click_to_summary(driver: WebDriver) -> None:
        DreamLandWorkerCase._click_with_confirm(
            driver,
            text="Naar besteloverzicht",
            label="payment_to_summary",
            confirm=lambda d: DreamLandWorkerCase._detect_checkout_step(d) == "summary",
            fallback_locators=[
                (By.CSS_SELECTOR, "div.checkout-payment-option__action button[type='submit']"),
                (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Naar besteloverzicht")),
            ],
            fallback_js_selectors=[
                "div.checkout-payment-option__action button[type='submit']",
            ],
            timeout=20,
        )

    @staticmethod
    def _accept_terms(driver: WebDriver) -> None:
        if DreamLandWorkerCase._set_checkbox_state(
            driver,
            "input#summary_termsAndConditionsAccepted",
            True,
        ):
            DreamLandWorkerCase._sleep(0.3)
            return

        clicked = DreamLandWorkerCase._try_click_locators(
            driver,
            [
                (By.CSS_SELECTOR, 'label[for="summary_termsAndConditionsAccepted"]'),
                (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Ik ga akkoord")),
            ],
            label="accept_terms",
            attempts=2,
            timeout=5,
        )
        if not clicked:
            raise RuntimeError("dreamland: summary terms checkbox not accepted")

    @staticmethod
    def _click_final_pay(driver: WebDriver) -> None:
        clicked = DreamLandWorkerCase._try_click_exact_text(
            driver,
            "Afrekenen met Creditcard",
            label="final_pay",
            attempts=4,
        )

        if not clicked:
            clicked = DreamLandWorkerCase._try_click_locators(
                driver,
                [
                    (By.CSS_SELECTOR, 'button[form="summary"]'),
                    (By.XPATH, DreamLandWorkerCase._contains_text_xpath("Afrekenen met Creditcard")),
                ],
                label="final_pay",
                attempts=2,
                timeout=6,
            )

        if not clicked:
            clicked = DreamLandWorkerCase._try_click_js_query(
                driver,
                [
                    'button[form="summary"]',
                ],
                label="final_pay",
            )

        if not clicked:
            raise RuntimeError("dreamland: final pay button not clicked")

        DreamLandWorkerCase._sleep(1.0)
        DreamLandWorkerCase._wait_dom_settle(driver, timeout=4.0)
        DreamLandWorkerCase._debug_page(driver, "after final_pay")

    @staticmethod
    def add_to_cart(
        driver: WebDriver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        timing = DreamLandWorkerCase._apply_timing(cfg)
        product_url = DreamLandWorkerCase._product_url(target)

        print(f"[dreamland] product_url={product_url}")
        if trace is not None:
            trace.step("Opening product page", {"phase": "product_page", "url": product_url})
        driver.get(product_url)
        DreamLandWorkerCase._wait_dom_settle(driver, timeout=4.5)
        DreamLandWorkerCase._sleep(timing.after_navigation_wait_seconds)
        DreamLandWorkerCase._debug_page(driver, "after product open")
        DreamLandWorkerCase._validate_product_purchasable(driver, trace)
        wait_if_queue(driver, site="dreamland", phase="after product page open", cfg=cfg, trace=trace)
        DreamLandWorkerCase._validate_product_purchasable(driver, trace)

        DreamLandWorkerCase._click_add_to_cart(driver, target, cfg, trace)
        if trace is not None:
            trace.step("Clicked add-to-cart", {"phase": "add_to_cart", "url": DreamLandWorkerCase._safe_current_url(driver)})
        DreamLandWorkerCase._sleep(timing.after_add_to_cart_wait_seconds)
        wait_if_queue(driver, site="dreamland", phase="after add_to_cart", cfg=cfg, trace=trace)

        try:
            DreamLandWorkerCase._wait_for_condition(
                driver,
                "add_to_cart",
                lambda d: DreamLandWorkerCase._detect_checkout_step(d) in {"modal", "cart", "details", "payment", "summary"},
                timeout=12,
            )
        except Exception:
            print("[dreamland] add_to_cart confirmation unclear, opening cart directly")
            if trace is not None:
                trace.warning("Add-to-cart confirmation unclear, opening cart directly", {"phase": "open_cart"})
            driver.get(DreamLandWorkerCase._cart_url(target))
            DreamLandWorkerCase._wait_dom_settle(driver, timeout=4.5)
            DreamLandWorkerCase._sleep(timing.after_navigation_wait_seconds)
            wait_if_queue(driver, site="dreamland", phase="after direct cart open", cfg=cfg, trace=trace)
            if "/cart" in DreamLandWorkerCase._safe_current_url(driver).lower():
                DreamLandWorkerCase._validate_cart_before_checkout(driver, target, trace)
            step = DreamLandWorkerCase._resolve_checkout_step_stable(driver, timeout=6)
            if step not in {"cart", "details", "payment", "summary"}:
                DreamLandWorkerCase._debug_snapshot(driver, "add_to_cart_unclear")
                raise RuntimeError(f"dreamland: add_to_cart did not reach cart flow, ended on step={step}")

    @staticmethod
    def checkout(
        driver: WebDriver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        timing = DreamLandWorkerCase._apply_timing(cfg)
        if trace is not None:
            trace.step("Checkout started", {"phase": "checkout", "url": DreamLandWorkerCase._safe_current_url(driver)})
        DreamLandWorkerCase._debug_page(driver, "checkout start")
        DreamLandWorkerCase._wait_dom_settle(driver, timeout=5.0)
        wait_if_queue(driver, site="dreamland", phase="checkout start", cfg=cfg, trace=trace)

        step = DreamLandWorkerCase._resolve_checkout_step_stable(driver, timeout=6)
        print(f"[dreamland] initial stable_step={step}")

        if step in {"unknown", "checkout_unknown"}:
            print("[dreamland] initial state unknown, opening cart directly")
            if trace is not None:
                trace.step("Opening cart directly", {"phase": "open_cart", "url": DreamLandWorkerCase._cart_url(target)})
            driver.get(DreamLandWorkerCase._cart_url(target))
            DreamLandWorkerCase._wait_dom_settle(driver, timeout=5.0)
            DreamLandWorkerCase._sleep(timing.after_navigation_wait_seconds)
            DreamLandWorkerCase._debug_page(driver, "after direct cart open")
            wait_if_queue(driver, site="dreamland", phase="after direct cart open", cfg=cfg, trace=trace)
            if "/cart" in DreamLandWorkerCase._safe_current_url(driver).lower():
                DreamLandWorkerCase._validate_cart_before_checkout(driver, target, trace)
            step = DreamLandWorkerCase._wait_checkout_actionable(driver, timeout=18)
            print(f"[dreamland] after direct cart open actionable_step={step}")

        max_rounds = 6

        for round_idx in range(1, max_rounds + 1):
            DreamLandWorkerCase._wait_dom_settle(driver, timeout=2.0)
            wait_if_queue(driver, site="dreamland", phase=f"checkout round {round_idx}", cfg=cfg, trace=trace)
            step = DreamLandWorkerCase._resolve_checkout_step_stable(driver, timeout=6)
            print(f"[dreamland] checkout round={round_idx} resolved_step={step}")

            if step == "modal":
                if trace is not None:
                    trace.step("Opening cart from add-to-cart modal", {"phase": "open_cart"})
                DreamLandWorkerCase._click_modal_to_cart(driver)
                continue

            if step == "cart":
                DreamLandWorkerCase._validate_cart_before_checkout(driver, target, trace)
                if trace is not None:
                    trace.step(
                        "Clicking cart checkout button",
                        {
                            "phase": "before_checkout",
                            "url": DreamLandWorkerCase._safe_current_url(driver),
                            "selector": "button[data-cart-continue] / Verder naar bestellen",
                        },
                    )
                DreamLandWorkerCase._click_cart_continue(driver)
                continue

            if step == "login":
                if trace is not None:
                    trace.step("Submitting login step if needed", {"phase": "login"})
                DreamLandWorkerCase._click_login_submit_if_needed(driver)
                continue

            if step == "details":
                if trace is not None:
                    trace.step("Filling checkout contact details", {"phase": "checkout_details"})
                DreamLandWorkerCase._fill_checkout_contact_fields(driver, cfg)
                DreamLandWorkerCase._ensure_same_delivery_address(driver)
                DreamLandWorkerCase._ensure_promotions_opt_out(driver)
                wait_if_queue(driver, site="dreamland", phase="before payment step click", cfg=cfg, trace=trace)
                DreamLandWorkerCase._click_continue_to_payment(driver)
                continue

            if step == "payment":
                if trace is not None:
                    trace.step("Selecting payment method and filling card data", {"phase": "payment"})
                DreamLandWorkerCase._select_creditcard(driver)
                DreamLandWorkerCase._fill_card_data(driver, cfg)
                wait_if_queue(driver, site="dreamland", phase="before summary step click", cfg=cfg, trace=trace)
                DreamLandWorkerCase._click_to_summary(driver)
                continue

            if step == "summary":
                if trace is not None:
                    trace.step("Accepting terms and preparing final payment click", {"phase": "final_submit"})
                DreamLandWorkerCase._accept_terms(driver)
                wait_if_queue(driver, site="dreamland", phase="before final submit", cfg=cfg, trace=trace)
                DreamLandWorkerCase._click_final_pay(driver)
                return

            if step in {"unknown", "checkout_unknown"}:
                current_url = DreamLandWorkerCase._safe_current_url(driver).lower()

                if "/cart" not in current_url and "/checkout" not in current_url:
                    print("[dreamland] unknown step outside cart/checkout, reopening cart")
                    if trace is not None:
                        trace.warning("Unknown checkout state, reopening cart", {"phase": "recovery"})
                    driver.get(DreamLandWorkerCase._cart_url(target))
                    DreamLandWorkerCase._wait_dom_settle(driver, timeout=5.0)
                    DreamLandWorkerCase._sleep(timing.after_navigation_wait_seconds)
                    wait_if_queue(driver, site="dreamland", phase="after recovery cart open", cfg=cfg, trace=trace)
                    continue

                if "/cart" in current_url:
                    print("[dreamland] unknown step inside cart, opening checkout directly")
                    if trace is not None:
                        trace.warning("Unknown cart state, opening checkout directly", {"phase": "recovery"})
                    driver.get(DreamLandWorkerCase._checkout_url(target))
                    DreamLandWorkerCase._wait_dom_settle(driver, timeout=5.0)
                    DreamLandWorkerCase._sleep(timing.after_navigation_wait_seconds)
                    wait_if_queue(driver, site="dreamland", phase="after recovery checkout open", cfg=cfg, trace=trace)
                    continue

                print("[dreamland] unknown checkout state, waiting one more cycle")
                DreamLandWorkerCase._sleep(1.0)
                continue

        DreamLandWorkerCase._debug_snapshot(driver, f"checkout_failed_{step}")
        raise RuntimeError(f"dreamland: checkout did not reach summary payment submit, ended on step={step}")

    @staticmethod
    def add_to_cart_and_checkout(
        driver: WebDriver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        DreamLandWorkerCase._apply_timing(cfg)
        DreamLandWorkerCase.add_to_cart(driver, target, cfg, trace)
        DreamLandWorkerCase.checkout(driver, target, cfg, trace)
        DreamLandWorkerCase._wait_dom_settle(driver, timeout=3.0)
        DreamLandWorkerCase._sleep(1.0)
