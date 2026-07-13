from __future__ import annotations

import time
from urllib.parse import urlsplit

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from pokemon_parser.config import AppConfig
from pokemon_parser.models import ActionTarget
from pokemon_parser.workers.base import BaseWorkerCase
from pokemon_parser.workers.queue import wait_if_queue
from pokemon_parser.workers.timing import build_worker_timing
from pokemon_parser.workers.trace import WorkerTraceLogger


class BolWorkerCase(BaseWorkerCase):
    @staticmethod
    def add_to_cart(
        driver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        if target.add_to_cart is None or not target.add_to_cart.add_to_cart_url:
            raise RuntimeError("bol add_to_cart: missing add_to_cart_url")

        timing = build_worker_timing(cfg)
        print(f"[bol] add_to_cart_url={target.add_to_cart.add_to_cart_url}")
        if trace is not None:
            trace.step("Opening Bol add-to-cart URL", {"phase": "add_to_cart", "url": target.add_to_cart.add_to_cart_url})
        driver.get(target.add_to_cart.add_to_cart_url)
        time.sleep(timing.after_add_to_cart_wait_seconds)
        wait_if_queue(driver, site="bol", phase="after add_to_cart URL", cfg=cfg, trace=trace)

        print(f"[bol] after add_to_cart current_url={driver.current_url}")
        print(f"[bol] after add_to_cart title={driver.title}")
        print(f"[bol] after add_to_cart cookies={len(driver.get_cookies())}")

    @staticmethod
    def _click(driver, xpath: str, cfg: AppConfig, timeout: float | None = None) -> None:
        timing = build_worker_timing(cfg)
        wait = WebDriverWait(
            driver,
            timeout or timing.wait_timeout_seconds,
            poll_frequency=timing.poll_seconds,
        )
        element = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(timing.click_pause_seconds)
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)
        time.sleep(timing.after_checkout_click_wait_seconds)

    @staticmethod
    def _is_login_page(driver) -> bool:
        try:
            parsed = urlsplit(str(driver.current_url or "").strip())
        except (AttributeError, ValueError):
            return False
        hostname = (parsed.hostname or "").rstrip(".").lower()
        path = parsed.path.rstrip("/").lower()
        if parsed.scheme.lower() != "https":
            return False
        if hostname == "login.bol.com":
            return True
        return hostname in {"bol.com", "www.bol.com"} and path.startswith("/wsp/login")

    @staticmethod
    def _click_login_submit_if_needed(
        driver,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
        timeout: float | None = None,
    ) -> bool:
        if not BolWorkerCase._is_login_page(driver):
            return False

        wait_if_queue(driver, site="bol", phase="before login submit", cfg=cfg, trace=trace)
        if trace is not None:
            trace.step("Submitting Bol login form", {"phase": "login", "url": driver.current_url})
        print(f"[bol] login page detected current_url={driver.current_url}")
        print(f"[bol] login page title={driver.title}")
        print(f"[bol] login page cookies={len(driver.get_cookies())}")

        login_xpaths = [
            "//button[@id='submit']",
            "//button[@type='SUBMIT' and normalize-space(.)='Inloggen']",
            "//button[@type='submit' and normalize-space(.)='Inloggen']",
            "//button[contains(., 'Inloggen')]",
        ]

        last_error = None
        for xpath in login_xpaths:
            try:
                print(f"[bol] trying login submit xpath={xpath}")
                BolWorkerCase._click(driver, xpath, cfg, timeout=timeout)
                print(f"[bol] after login submit current_url={driver.current_url}")
                print(f"[bol] after login submit title={driver.title}")
                print(f"[bol] after login submit cookies={len(driver.get_cookies())}")
                return True
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"bol login: 'Inloggen' button not found ({last_error})")

    @staticmethod
    def _go_from_basket_to_checkout(
        driver,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        wait_if_queue(driver, site="bol", phase="before basket checkout", cfg=cfg, trace=trace)
        if trace is not None:
            trace.step("Clicking basket checkout button", {"phase": "before_checkout", "url": driver.current_url})
        print(f"[bol] basket current_url={driver.current_url}")
        print(f"[bol] basket title={driver.title}")
        print(f"[bol] basket cookies={len(driver.get_cookies())}")

        basket_checkout_xpaths = [
            "//button[normalize-space(.)='Verder naar bestellen']",
            "//a[normalize-space(.)='Verder naar bestellen']",
            "//button[contains(., 'Verder naar bestellen')]",
            "//a[contains(., 'Verder naar bestellen')]",
        ]

        last_error = None
        for xpath in basket_checkout_xpaths:
            try:
                print(f"[bol] trying basket checkout xpath={xpath}")
                BolWorkerCase._click(driver, xpath, cfg, timeout=5.0)
                print(f"[bol] after basket click current_url={driver.current_url}")
                print(f"[bol] after basket click title={driver.title}")
                print(f"[bol] after basket click cookies={len(driver.get_cookies())}")
                return
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"bol basket->checkout: checkout button not found ({last_error})")

    @staticmethod
    def _click_bestellen_en_betalen(
        driver,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
        timeout: float | None = None,
    ) -> None:
        wait_if_queue(driver, site="bol", phase="before final submit", cfg=cfg, trace=trace)
        if trace is not None:
            trace.step("Clicking Bestellen en betalen", {"phase": "final_submit", "url": driver.current_url})
        print(f"[bol] before bestellen current_url={driver.current_url}")
        print(f"[bol] before bestellen title={driver.title}")
        print(f"[bol] before bestellen cookies={len(driver.get_cookies())}")

        xpaths = [
            "//button[normalize-space(.)='Bestellen en betalen']",
            "//button[contains(., 'Bestellen en betalen')]",
            "//a[normalize-space(.)='Bestellen en betalen']",
            "//a[contains(., 'Bestellen en betalen')]",
        ]

        last_error = None
        for xpath in xpaths:
            try:
                print(f"[bol] trying bestellen xpath={xpath}")
                BolWorkerCase._click(driver, xpath, cfg, timeout=timeout)
                print(f"[bol] after bestellen current_url={driver.current_url}")
                print(f"[bol] after bestellen title={driver.title}")
                print(f"[bol] after bestellen cookies={len(driver.get_cookies())}")
                return
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"bol checkout: 'Bestellen en betalen' button not found ({last_error})")

    @staticmethod
    def _wait_until_not_login(driver, cfg: AppConfig, timeout: float | None = None) -> None:
        timing = build_worker_timing(cfg)
        timeout = timeout or timing.wait_timeout_seconds
        end_time = time.time() + timeout
        while time.time() < end_time:
            if not BolWorkerCase._is_login_page(driver):
                return
            time.sleep(max(0.1, timing.poll_seconds))
        raise RuntimeError("bol login: still on login page after clicking 'Inloggen'")

    @staticmethod
    def checkout(
        driver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        timing = build_worker_timing(cfg)
        if "/basket" not in driver.current_url:
            print("[bol] not on basket, opening basket directly")
            if trace is not None:
                trace.step("Opening Bol basket directly", {"phase": "open_cart", "url": "https://www.bol.com/nl/nl/basket/"})
            driver.get("https://www.bol.com/nl/nl/basket/")
            time.sleep(timing.after_navigation_wait_seconds)
            wait_if_queue(driver, site="bol", phase="after basket open", cfg=cfg, trace=trace)

        print(f"[bol] checkout start current_url={driver.current_url}")
        if trace is not None:
            trace.step("Bol checkout started", {"phase": "checkout", "url": driver.current_url})
        wait_if_queue(driver, site="bol", phase="checkout start", cfg=cfg, trace=trace)

        BolWorkerCase._go_from_basket_to_checkout(driver, cfg, trace)

        if BolWorkerCase._is_login_page(driver):
            print("[bol] redirected to login after basket checkout click, trying submit")
            BolWorkerCase._click_login_submit_if_needed(driver, cfg, trace)
            BolWorkerCase._wait_until_not_login(driver, cfg, timeout=20.0)
            wait_if_queue(driver, site="bol", phase="after login submit", cfg=cfg, trace=trace)

        BolWorkerCase._click_bestellen_en_betalen(driver, cfg, trace)

        if BolWorkerCase._is_login_page(driver):
            print("[bol] redirected to login after 'Bestellen en betalen', trying submit")
            BolWorkerCase._click_login_submit_if_needed(driver, cfg, trace)
            BolWorkerCase._wait_until_not_login(driver, cfg, timeout=20.0)

    @staticmethod
    def add_to_cart_and_checkout(
        driver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        timing = build_worker_timing(cfg)
        BolWorkerCase.add_to_cart(driver, target, cfg, trace)
        time.sleep(timing.after_add_to_cart_wait_seconds)
        wait_if_queue(driver, site="bol", phase="before checkout", cfg=cfg, trace=trace)
        BolWorkerCase.checkout(driver, target, cfg, trace)
