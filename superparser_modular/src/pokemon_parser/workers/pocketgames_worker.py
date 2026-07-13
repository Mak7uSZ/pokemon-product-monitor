from __future__ import annotations

import time
from urllib.parse import urlparse

from selenium.webdriver.common.by import By

from pokemon_parser.config import AppConfig
from pokemon_parser.models import ActionTarget
from pokemon_parser.workers.base import BaseWorkerCase
from pokemon_parser.workers.purchase_safety import (
    PURCHASE_STATUS_ADDED_TO_CART,
    PURCHASE_STATUS_CHECKOUT_STARTED,
    PURCHASE_STATUS_CONFIRMED,
    PURCHASE_STATUS_FAILED,
    PURCHASE_STATUS_PAYMENT_SUBMITTED,
    PURCHASE_STATUS_UNKNOWN_REVIEW,
    purchase_key_for_target,
)
from pokemon_parser.workers.queue import detect_queue_page, wait_if_queue
from pokemon_parser.workers.timing import build_worker_timing
from pokemon_parser.workers.trace import WorkerTraceLogger


class PocketGamesWorkerCase(BaseWorkerCase):
    BASE_URL = "https://pocketgames.nl"
    DEFAULT_CART_URL = f"{BASE_URL}/cart"
    DEFAULT_CART_ADD_URL = f"{BASE_URL}/cart/add"
    DEFAULT_CHECKOUT_URL = f"{BASE_URL}/checkout"
    DEFAULT_SECTION_ID = "template--17386837573891__main"
    DEFAULT_SECTIONS = "cart-drawer,cart-drawer,cart-drawer,cart-drawer"
    DEFAULT_SECTIONS_URL = "/collections/pokemon"

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def _quantity(target: ActionTarget) -> int:
        try:
            return max(1, int((target.add_to_cart.quantity if target.add_to_cart else 1) or 1))
        except Exception:
            return 1

    @staticmethod
    def _variant_id(target: ActionTarget) -> int:
        if target.add_to_cart is None or not target.add_to_cart.variant_id:
            raise RuntimeError("pocketgames: missing variant_id")
        return int(target.add_to_cart.variant_id)

    @staticmethod
    def _product_id(target: ActionTarget):
        if target.add_to_cart is None:
            return None
        return target.add_to_cart.product_id

    @staticmethod
    def _product_url(target: ActionTarget) -> str:
        return target.product_url or PocketGamesWorkerCase.BASE_URL

    @staticmethod
    def _cart_url(target: ActionTarget) -> str:
        if target.add_to_cart and target.add_to_cart.cart_url:
            return target.add_to_cart.cart_url
        if target.checkout and target.checkout.cart_url:
            return target.checkout.cart_url
        return PocketGamesWorkerCase.DEFAULT_CART_URL

    @staticmethod
    def _checkout_url(target: ActionTarget) -> str:
        if target.checkout and target.checkout.checkout_url:
            return target.checkout.checkout_url
        return PocketGamesWorkerCase.DEFAULT_CHECKOUT_URL

    @staticmethod
    def _sections_url(target: ActionTarget) -> str:
        if target.add_to_cart and target.add_to_cart.sections_url:
            return target.add_to_cart.sections_url
        product_url = PocketGamesWorkerCase._product_url(target)
        try:
            parsed = urlparse(product_url)
            if parsed.path:
                return parsed.path
        except Exception:
            pass
        return PocketGamesWorkerCase.DEFAULT_SECTIONS_URL

    @staticmethod
    def _safe_url(driver) -> str:
        try:
            return driver.current_url
        except Exception:
            return "unknown_url"

    @staticmethod
    def _page_text(driver) -> str:
        try:
            return str(
                driver.execute_script(
                    "return (document.body && document.body.innerText) ? document.body.innerText : '';"
                )
                or ""
            )
        except Exception:
            try:
                return str(driver.page_source or "")
            except Exception:
                return ""

    @staticmethod
    def _exists(driver, by, selector) -> bool:
        try:
            return len(driver.find_elements(by, selector)) > 0
        except Exception:
            return False

    @staticmethod
    def _click_js(driver, element) -> bool:
        try:
            driver.execute_script("arguments[0].click();", element)
            return True
        except Exception:
            return False

    @staticmethod
    def _click_first_matching(driver, selectors: list[tuple[str, str]], timeout: float = 5.0) -> bool:
        started = time.time()
        while time.time() - started < timeout:
            for by_name, selector in selectors:
                by = getattr(By, by_name)
                try:
                    elements = driver.find_elements(by, selector)
                    for el in elements:
                        try:
                            if el.is_displayed():
                                try:
                                    el.click()
                                    return True
                                except Exception:
                                    if PocketGamesWorkerCase._click_js(driver, el):
                                        return True
                        except Exception:
                            continue
                except Exception:
                    continue
            time.sleep(0.1)
        return False

    @staticmethod
    def _find_checkout_ready(driver) -> bool:
        try:
            url = PocketGamesWorkerCase._safe_url(driver).lower()
            page = driver.page_source.lower()

            if "checkout" in url:
                return True

            ready_signs = [
                'id="email"',
                'name="firstName"',
                'name="lastName"',
                'name="streetName"',
                'name="streetNumber"',
                'name="postalCode"',
                'name="city"',
            ]
            return any(sign in page for sign in ready_signs)
        except Exception:
            return False

    @staticmethod
    def _is_queue_page(driver) -> bool:
        return detect_queue_page(driver, "pocketgames").in_queue

    @staticmethod
    def _wait_for_checkout_or_queue(driver, timeout: float = 40.0) -> str:
        started = time.time()
        while time.time() - started < timeout:
            if PocketGamesWorkerCase._find_checkout_ready(driver):
                return "checkout_ready"
            if PocketGamesWorkerCase._is_queue_page(driver):
                return "queue"
            time.sleep(0.15)
        return "timeout"

    @staticmethod
    def _wait_until_queue_passes(driver, timeout: float = 300.0) -> str:
        started = time.time()
        while time.time() - started < timeout:
            if PocketGamesWorkerCase._find_checkout_ready(driver):
                return "checkout_ready"
            time.sleep(0.5)
        return "timeout"

    @staticmethod
    def _fast_fill(driver, selector: str, value: str) -> None:
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].value = arguments[1];", el, value)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", el)
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", el)
        except Exception:
            pass

    @staticmethod
    def _fill_checkout(driver, cfg: AppConfig) -> None:
        PocketGamesWorkerCase._fast_fill(driver, "input#email", getattr(cfg, "checkout_email", ""))
        PocketGamesWorkerCase._fast_fill(driver, 'input[name="firstName"]', getattr(cfg, "checkout_first_name", ""))
        PocketGamesWorkerCase._fast_fill(driver, 'input[name="lastName"]', getattr(cfg, "checkout_last_name", ""))
        PocketGamesWorkerCase._fast_fill(driver, 'input[name="streetName"]', getattr(cfg, "checkout_street", ""))
        PocketGamesWorkerCase._fast_fill(driver, 'input[name="streetNumber"]', getattr(cfg, "checkout_house_number", ""))
        PocketGamesWorkerCase._fast_fill(driver, 'input[name="postalCode"]', getattr(cfg, "checkout_zip_code", ""))
        PocketGamesWorkerCase._fast_fill(driver, 'input[name="city"]', getattr(cfg, "checkout_city", ""))

    @staticmethod
    def _select_card_method(driver) -> None:
        try:
            driver.execute_script("document.getElementById('basic-creditCards')?.click();")
        except Exception:
            pass

    @staticmethod
    def _fill_card_frames(driver, cfg: AppConfig) -> None:
        started = time.time()
        while time.time() - started < 12:
            if driver.find_elements(By.CSS_SELECTOR, 'iframe[id^="card-fields-number"]'):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("pocketgames: card iframes not ready")

        card_fields = [
            ('iframe[id^="card-fields-number"]', 'input#number', getattr(cfg, "checkout_card_number", "")),
            ('iframe[id^="card-fields-expiry"]', 'input#expiry', getattr(cfg, "checkout_card_expiry", "")),
            ('iframe[id^="card-fields-verification_value"]', 'input#verification_value', getattr(cfg, "checkout_card_cvv", "")),
            ('iframe[id^="card-fields-name"]', 'input#name', getattr(cfg, "checkout_card_name", "")),
        ]

        for frame_sel, input_sel, value in card_fields:
            frame = driver.find_element(By.CSS_SELECTOR, frame_sel)
            driver.switch_to.frame(frame)
            PocketGamesWorkerCase._fast_fill(driver, input_sel, value)
            driver.switch_to.default_content()

    @staticmethod
    def _click_pay(driver) -> None:
        clicked = False
        try:
            clicked = bool(
                driver.execute_script(
                    """
                    const button = document.getElementById('checkout-pay-button');
                    if (!button) {
                        return false;
                    }
                    button.scrollIntoView({ block: 'center', inline: 'center' });
                    button.click();
                    return true;
                    """
                )
            )
        except Exception:
            clicked = False

        if not clicked:
            raise RuntimeError("pocketgames: pay button not found or not clicked")

    @staticmethod
    def _record_purchase_state(
        target: ActionTarget,
        trace: WorkerTraceLogger | None,
        status: str,
        *,
        driver=None,
        confirmation_signal: str | None = None,
        error_message: str | None = None,
        details: dict | None = None,
    ) -> None:
        if trace is None or trace.storage is None:
            return

        purchase_key = purchase_key_for_target(target)
        final_url_statuses = {
            PURCHASE_STATUS_PAYMENT_SUBMITTED,
            PURCHASE_STATUS_CONFIRMED,
            PURCHASE_STATUS_FAILED,
            PURCHASE_STATUS_UNKNOWN_REVIEW,
        }
        trace.storage.update_purchase_state(
            site=target.site,
            purchase_key=purchase_key,
            status=status,
            external_id=target.external_id,
            title=target.title,
            product_url=target.product_url,
            confirmation_url=(
                PocketGamesWorkerCase._safe_url(driver)
                if driver is not None and status in final_url_statuses
                else None
            ),
            confirmation_signal=confirmation_signal,
            error_message=error_message,
            details={
                "purchase_key": purchase_key,
                **(details or {}),
            },
        )

    @staticmethod
    def _detect_purchase_result(driver) -> tuple[str, str]:
        url = PocketGamesWorkerCase._safe_url(driver)
        lowered_url = url.lower()
        text = PocketGamesWorkerCase._page_text(driver).lower()
        try:
            source = (driver.page_source or "").lower()
        except Exception:
            source = ""

        failure_phrases = (
            "betaling mislukt",
            "betaling geweigerd",
            "payment failed",
            "payment was declined",
            "card was declined",
            "kaart geweigerd",
            "kon niet worden verwerkt",
            "could not be processed",
        )
        for phrase in failure_phrases:
            if phrase in text or phrase in source:
                return PURCHASE_STATUS_FAILED, f"text:{phrase}"

        confirmation_selectors = (
            "[data-step='thank_you']",
            "[data-step='thank-you']",
            "[class*='thank-you']",
            "[id*='thank-you']",
            "[class*='order-confirmation']",
            "[id*='order-confirmation']",
            ".os-order-number",
            "[class*='order-number']",
            "[id*='order-number']",
        )
        for selector in confirmation_selectors:
            if PocketGamesWorkerCase._exists(driver, By.CSS_SELECTOR, selector):
                return PURCHASE_STATUS_CONFIRMED, f"dom:{selector}"

        text_phrases = (
            "bedankt voor je bestelling",
            "bedankt voor uw bestelling",
            "bestelling bevestigd",
            "order bevestigd",
            "order confirmation",
            "thank you for your order",
            "thank you",
            "order number",
            "bestelnummer",
            "bedankt",
        )
        for phrase in text_phrases:
            if phrase in text or phrase in source:
                return PURCHASE_STATUS_CONFIRMED, f"text:{phrase}"

        dom_markers = (
            "checkout-thank-you",
            "thank_you",
            "thank-you",
            "os-order-number",
            "order-number",
            "bestelnummer",
        )
        for marker in dom_markers:
            if marker in source:
                return PURCHASE_STATUS_CONFIRMED, f"dom_text:{marker}"

        strong_url_signals = ("thank", "thanks", "bedankt", "confirmation", "success")
        for signal in strong_url_signals:
            if signal in lowered_url:
                return PURCHASE_STATUS_CONFIRMED, f"url:{signal}"

        weak_url_signals = ("order", "bestelling")
        for signal in weak_url_signals:
            if signal in lowered_url and any(
                marker in text or marker in source
                for marker in ("bedankt", "bevestigd", "confirmed", "confirmation", "bestelnummer", "order number")
            ):
                return PURCHASE_STATUS_CONFIRMED, f"url_text:{signal}"

        queue_state = detect_queue_page(driver, "pocketgames")
        if queue_state.in_queue:
            return PURCHASE_STATUS_UNKNOWN_REVIEW, "queue_after_submit:" + ",".join(queue_state.signals)

        return PURCHASE_STATUS_UNKNOWN_REVIEW, "confirmation_not_detected"

    @staticmethod
    def _wait_for_purchase_result(driver, *, timeout: float, poll_seconds: float) -> tuple[str, str]:
        started = time.monotonic()
        last_signal = "confirmation_not_detected"
        while time.monotonic() - started < timeout:
            status, signal = PocketGamesWorkerCase._detect_purchase_result(driver)
            last_signal = signal
            if status != PURCHASE_STATUS_UNKNOWN_REVIEW or signal.startswith("queue_after_submit:"):
                return status, signal
            time.sleep(max(0.1, poll_seconds))
        return PURCHASE_STATUS_UNKNOWN_REVIEW, last_signal

    @staticmethod
    def _open_fast_cart(driver, target: ActionTarget) -> None:
        variant_id = PocketGamesWorkerCase._variant_id(target)
        qty = PocketGamesWorkerCase._quantity(target)
        driver.get(f"{PocketGamesWorkerCase.BASE_URL}/cart/{variant_id}:{qty}")

    @staticmethod
    def _fallback_form_add(driver, target: ActionTarget) -> None:
        add = target.add_to_cart
        if add is None:
            raise RuntimeError("pocketgames: add_to_cart target missing")

        product_id = PocketGamesWorkerCase._product_id(target)
        if not product_id:
            raise RuntimeError("pocketgames: missing product_id for fallback_form_add")

        variant_id = PocketGamesWorkerCase._variant_id(target)
        quantity = PocketGamesWorkerCase._quantity(target)
        cart_add_url = add.cart_add_url or PocketGamesWorkerCase.DEFAULT_CART_ADD_URL
        cart_url = add.cart_url or PocketGamesWorkerCase.DEFAULT_CART_URL
        product_url = add.product_url or PocketGamesWorkerCase._product_url(target)
        section_id = add.section_id or PocketGamesWorkerCase.DEFAULT_SECTION_ID
        sections_url = PocketGamesWorkerCase._sections_url(target)

        driver.get(product_url)
        time.sleep(0.15)

        result = driver.execute_async_script(
            """
            const done = arguments[arguments.length - 1];

            const cartAddUrl = arguments[0];
            const variantId = String(arguments[1]);
            const productId = String(arguments[2]);
            const quantity = Number(arguments[3] || 1);
            const sectionId = arguments[4];
            const sectionsUrl = arguments[5];
            const sections = arguments[6];

            (async () => {
                try {
                    const form = new FormData();
                    form.append("form_type", "product");
                    form.append("utf8", "✓");
                    form.append("id", variantId);
                    form.append("product-id", productId);
                    form.append("section-id", sectionId);
                    form.append("sections", sections);
                    form.append("sections_url", sectionsUrl);
                    if (quantity > 1) {
                        form.append("quantity", String(quantity));
                    }

                    const response = await fetch(cartAddUrl, {
                        method: "POST",
                        credentials: "include",
                        headers: {
                            "Accept": "application/javascript",
                            "X-Requested-With": "XMLHttpRequest"
                        },
                        body: form
                    });

                    const body = await response.text();
                    done({ ok: response.ok, status: response.status, body });
                } catch (e) {
                    done({ ok: false, status: 0, body: String(e) });
                }
            })();
            """,
            cart_add_url,
            variant_id,
            product_id,
            quantity,
            section_id,
            sections_url,
            PocketGamesWorkerCase.DEFAULT_SECTIONS,
        )

        if not result or not result.get("ok"):
            raise RuntimeError(f"pocketgames fallback_form_add failed: {result}")

        driver.get(cart_url)

    @staticmethod
    def _click_cart_buttons(driver) -> None:
        # 1. Snel Afrekenen
        clicked_fast = PocketGamesWorkerCase._click_first_matching(driver, [
            ("CSS_SELECTOR", "button[name='checkout']"),
            ("CSS_SELECTOR", "button[type='submit'][name='checkout']"),
            ("XPATH", "//button[contains(., 'Snel Afrekenen')]"),
            ("XPATH", "//button[contains(., 'Snel afrekenen')]"),
            ("XPATH", "//button[contains(., 'Fast checkout')]"),
        ], timeout=6.0)

        if not clicked_fast:
            # Иногда на некоторых темах можно сразу попасть по checkout-link
            try:
                driver.get(PocketGamesWorkerCase.DEFAULT_CHECKOUT_URL)
                return
            except Exception:
                pass

        time.sleep(0.4)

        # 2. Afrekenen
        PocketGamesWorkerCase._click_first_matching(driver, [
            ("XPATH", "//button[contains(., 'Afrekenen')]"),
            ("XPATH", "//a[contains(., 'Afrekenen')]"),
            ("XPATH", "//button[contains(., 'Checkout')]"),
            ("XPATH", "//a[contains(., 'Checkout')]"),
        ], timeout=5.0)

    @staticmethod
    def add_to_cart(
        driver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        if trace is not None:
            trace.step("Opening fast cart", {"phase": "add_to_cart", "url": PocketGamesWorkerCase._product_url(target)})
        try:
            PocketGamesWorkerCase._open_fast_cart(driver, target)
        except Exception:
            if trace is not None:
                trace.warning("Fast cart failed, using form fallback", {"phase": "add_to_cart"})
            PocketGamesWorkerCase._fallback_form_add(driver, target)
        wait_if_queue(driver, site="pocketgames", phase="after add_to_cart", cfg=cfg, trace=trace)
        PocketGamesWorkerCase._record_purchase_state(
            target,
            trace,
            PURCHASE_STATUS_ADDED_TO_CART,
            driver=driver,
            details={"phase": "after_add_to_cart"},
        )

    @staticmethod
    def checkout(
        driver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        timing = build_worker_timing(cfg)
        PocketGamesWorkerCase.add_to_cart(driver, target, cfg, trace)
        time.sleep(timing.after_add_to_cart_wait_seconds)
        wait_if_queue(driver, site="pocketgames", phase="before checkout buttons", cfg=cfg, trace=trace)
        if trace is not None:
            trace.step("Clicking cart checkout buttons", {"phase": "before_checkout", "url": PocketGamesWorkerCase._safe_url(driver)})
        PocketGamesWorkerCase._click_cart_buttons(driver)
        PocketGamesWorkerCase._record_purchase_state(
            target,
            trace,
            PURCHASE_STATUS_CHECKOUT_STARTED,
            driver=driver,
            details={"phase": "checkout_started"},
        )

    @staticmethod
    def add_to_cart_and_checkout(
        driver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        timing = build_worker_timing(cfg)
        PocketGamesWorkerCase.checkout(driver, target, cfg, trace)

        state = PocketGamesWorkerCase._wait_for_checkout_or_queue(driver, timeout=timing.wait_timeout_seconds * 2)
        if state == "queue":
            wait_if_queue(driver, site="pocketgames", phase="checkout queue", cfg=cfg, trace=trace)
            state = PocketGamesWorkerCase._wait_for_checkout_or_queue(driver, timeout=timing.wait_timeout_seconds)

        if state != "checkout_ready":
            raise RuntimeError(
                f"pocketgames: checkout not ready, state={state}, url={PocketGamesWorkerCase._safe_url(driver)}"
            )

        if trace is not None:
            trace.step("Filling checkout details", {"phase": "checkout_details", "url": PocketGamesWorkerCase._safe_url(driver)})
        wait_if_queue(driver, site="pocketgames", phase="before checkout form fill", cfg=cfg, trace=trace)
        PocketGamesWorkerCase._fill_checkout(driver, cfg)
        if trace is not None:
            trace.step("Selecting card payment", {"phase": "payment"})
        PocketGamesWorkerCase._select_card_method(driver)
        wait_if_queue(driver, site="pocketgames", phase="before card frames", cfg=cfg, trace=trace)
        PocketGamesWorkerCase._fill_card_frames(driver, cfg)
        wait_if_queue(driver, site="pocketgames", phase="before final pay", cfg=cfg, trace=trace)
        if trace is not None:
            trace.step("Clicking pay button", {"phase": "final_submit"})
        PocketGamesWorkerCase._click_pay(driver)
        PocketGamesWorkerCase._record_purchase_state(
            target,
            trace,
            PURCHASE_STATUS_PAYMENT_SUBMITTED,
            driver=driver,
            details={"phase": "payment_submitted"},
        )

        status, signal = PocketGamesWorkerCase._wait_for_purchase_result(
            driver,
            timeout=max(20.0, timing.wait_timeout_seconds * 2),
            poll_seconds=timing.poll_seconds,
        )
        metadata = {
            "phase": "purchase_confirmation",
            "url": PocketGamesWorkerCase._safe_url(driver),
            "confirmation_signal": signal,
            "status": status,
            "purchase_key": purchase_key_for_target(target),
        }

        if status == PURCHASE_STATUS_CONFIRMED:
            PocketGamesWorkerCase._record_purchase_state(
                target,
                trace,
                PURCHASE_STATUS_CONFIRMED,
                driver=driver,
                confirmation_signal=signal,
                details=metadata,
            )
            if trace is not None:
                trace.set_result(PURCHASE_STATUS_CONFIRMED, metadata)
                trace.success("PocketGames purchase confirmed", metadata)
            return

        if status == PURCHASE_STATUS_FAILED:
            PocketGamesWorkerCase._record_purchase_state(
                target,
                trace,
                PURCHASE_STATUS_FAILED,
                driver=driver,
                confirmation_signal=signal,
                error_message=signal,
                details=metadata,
            )
            if trace is not None:
                trace.set_result(PURCHASE_STATUS_FAILED, metadata)
            raise RuntimeError(f"pocketgames: payment failed signal={signal} url={PocketGamesWorkerCase._safe_url(driver)}")

        PocketGamesWorkerCase._record_purchase_state(
            target,
            trace,
            PURCHASE_STATUS_UNKNOWN_REVIEW,
            driver=driver,
            confirmation_signal=signal,
            error_message="final submit clicked but confirmation was not detected",
            details=metadata,
        )
        if trace is not None:
            if signal.startswith("queue_after_submit:"):
                trace.warning(
                    "Queue detected after final submit",
                    {
                        **metadata,
                        "waiting_up_to_seconds": cfg.queue_wait_timeout_seconds,
                    },
                    level="minimal",
                )
            trace.set_result(PURCHASE_STATUS_UNKNOWN_REVIEW, metadata)
            trace.warning(
                "PocketGames result unknown after final submit. Manual review required. Auto-retry blocked.",
                metadata,
                level="minimal",
            )
