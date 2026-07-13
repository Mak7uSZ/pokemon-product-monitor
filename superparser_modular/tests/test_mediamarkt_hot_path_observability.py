from __future__ import annotations

import json
import queue
from types import SimpleNamespace

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from pokemon_parser.models import ActionTarget, SeleniumJob
from pokemon_parser.workers.mediamarkt_worker import MediaMarktCheckoutUnavailableError, MediaMarktWorkerCase
from pokemon_parser.workers.observability import WorkerActionTimeline, timed_click, timed_wait
from pokemon_parser.workers.selenium_worker import SeleniumWorker


class _Element:
    tag_name = "button"

    def __init__(
        self,
        driver,
        *,
        text="Ik wil bestellen",
        element_id="pdp-add-to-cart-button",
        data_test="cofr-add-to-basket-button",
        visible=True,
        enabled=True,
        rect=None,
    ):
        self.driver = driver
        self.text = text
        self.element_id = element_id
        self.data_test = data_test
        self.visible = visible
        self.enabled = enabled
        self.click_calls = 0
        self.rect = rect or {"x": 10, "y": 20, "width": 190 if visible else 0, "height": 48 if visible else 0}

    def get_attribute(self, name):
        return {
            "id": self.element_id,
            "data-test": self.data_test,
            "aria-label": self.text,
            "class": "primary-button",
            "href": "",
            "disabled": "" if self.enabled else "disabled",
            "aria-disabled": "false" if self.enabled else "true",
        }.get(name, "")

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def click(self):
        self.click_calls += 1
        self.driver.clicked = True


class _Driver:
    title = "MediaMarkt PDP"
    current_window_handle = "main"
    page_source = "<html></html>"

    def __init__(
        self,
        *,
        real_button=True,
        unsafe_button=False,
        unavailable_text="",
        visible_unavailable=False,
        disabled_checkout=False,
        hidden_duplicate=False,
    ):
        self.current_url = "https://www.mediamarkt.nl/nl/product/_pokemon-1895844.html"
        self.hidden_duplicate = hidden_duplicate
        self.hidden_button = _Element(
            self,
            element_id="pdp-add-to-cart-button",
            data_test="cofr-add-to-basket-button a2c-Button",
            visible=False,
            rect={"x": 0, "y": 0, "width": 0, "height": 0},
        )
        self.button = _Element(
            self,
            element_id="" if hidden_duplicate else "pdp-add-to-cart-button",
            data_test="cofr-add-to-basket-button a2c-Button",
            rect={"x": 263, "y": 597, "width": 606, "height": 48},
        )
        self.real_button = real_button
        self.unsafe_button = unsafe_button
        self.unavailable_text = unavailable_text
        self.visible_unavailable = visible_unavailable
        self.disabled_checkout = disabled_checkout
        self.clicked = False
        self.execute_script_calls = []
        self.find_calls = []
        self.screenshots = []

    def _negative_markers(self):
        lowered = self.unavailable_text.lower()
        return [
            phrase
            for phrase in (
                "niet verkrijgbaar",
                "helaas geen bezorging mogelijk",
                "dit product is binnenkort weer beschikbaar",
            )
            if phrase in lowered
        ]

    def _button_summary(self, button=None):
        button = button or self.button
        return {
            "tag": "button",
            "text": button.text,
            "id": button.element_id,
            "data_test": button.data_test,
            "aria_label": button.text,
            "visible": button.visible,
            "enabled": button.enabled,
            "disabled_attr": button.get_attribute("disabled"),
            "aria_disabled": button.get_attribute("aria-disabled"),
            "rect": button.rect,
        }

    def execute_script(self, script, *args):
        self.execute_script_calls.append((script, args))
        if "document.readyState" in script:
            return "complete"
        if "arguments[0].click" in script and args:
            args[0].click()
            return None
        if "scrollIntoView" in script:
            return None
        if "document.body && document.body.innerText" in script and "toLowerCase" not in script:
            return self.unavailable_text
        if "negativePhrases" in script or "availabilityDecision" in script:
            negative_markers = self._negative_markers()
            if self.unsafe_button:
                return {
                    "ok": False,
                    "reason": "buy_button_missing",
                    "button": None,
                    "availability_markers": ["text=Online op voorraad"],
                    "rejection_markers": [],
                    "ignored_negative_markers": negative_markers,
                    "rejected_buttons": [{"reason": "unsafe:meldingen activeren"}],
                    "availability_decision": {
                        "decision": "unavailable",
                        "reason": "buy_button_missing",
                        "positive_markers": ["text=Online op voorraad"],
                        "ignored_negative_markers": negative_markers,
                    },
                }
            if self.visible_unavailable and not self.real_button:
                return {
                    "ok": False,
                    "reason": "visible_unavailable_status_without_positive_override",
                    "button": None,
                    "button_summary": None,
                    "availability_markers": [],
                    "rejection_markers": negative_markers or ["niet verkrijgbaar"],
                    "ignored_negative_markers": [],
                    "rejected_buttons": [],
                    "availability_decision": {
                        "decision": "unavailable",
                        "reason": "visible_unavailable_status_without_positive_override",
                        "positive_markers": [],
                        "rejection_markers": negative_markers or ["niet verkrijgbaar"],
                    },
                }
            ok = self.real_button
            rejected_buttons = []
            if self.hidden_duplicate:
                rejected_buttons.append({"reason": "not_visible", "summary": self._button_summary(self.hidden_button)})
            return {
                "ok": ok,
                "reason": "ready" if ok else "buy_button_missing",
                "button": self.button if ok else None,
                "button_summary": self._button_summary() if ok else None,
                "availability_markers": [
                    "data-product-online-status=AVAILABLE",
                    "data-test=mms-cofr-delivery_AVAILABLE",
                    "text=Online op voorraad",
                    "button=visible_enabled_Ik wil bestellen",
                ],
                "rejection_markers": [],
                "ignored_negative_markers": negative_markers,
                "ignored_negative_reason": "found_only_in_full_html_or_translation_script" if negative_markers else "",
                "rejected_buttons": rejected_buttons,
                "availability_decision": {
                    "decision": "available" if ok else "unavailable",
                    "reason": "strong_positive_available_delivery_and_visible_enabled_button" if ok else "buy_button_missing",
                    "positive_markers": [
                        "data-product-online-status=AVAILABLE",
                        "data-test=mms-cofr-delivery_AVAILABLE",
                        "text=Online op voorraad",
                        "button=visible_enabled_Ik wil bestellen",
                    ],
                    "ignored_negative_markers": negative_markers,
                    "ignored_negative_reason": "found_only_in_full_html_or_translation_script" if negative_markers else "",
                },
            }
        if "disabledCheckoutButtons" in script:
            markers = []
            lowered = self.unavailable_text.lower()
            if self.visible_unavailable or self.disabled_checkout:
                for phrase in (
                    "een of meer producten zijn niet langer verkrijgbaar",
                    "jouw winkelwagen is gewijzigd",
                    "helaas zijn deze producten niet verkrijgbaar",
                    "niet verkrijgbaar",
                    "niet langer verkrijgbaar",
                    "helaas geen bezorging mogelijk",
                    "dit product is binnenkort weer beschikbaar",
                ):
                    if phrase in lowered:
                        markers.append(phrase)
            disabled = []
            if self.disabled_checkout:
                disabled.append({"text": "Ik ga bestellen", "aria_disabled": "true"})
            return {"markers": markers, "disabled_checkout_buttons": disabled}
        if "querySelectorAll('button,a[role=\"button\"],a[href]')" in script:
            return [{"text": "Ik wil bestellen", "id": "pdp-add-to-cart-button", "visible": True}]
        if "const selectors = [" in script:
            return [{"text": self.unavailable_text, "selector": "[role=alert]"}] if self.unavailable_text else []
        if "querySelectorAll('h1,h2,h3" in script:
            return ["Checkout"]
        if "cart_total" in script:
            return {"cart_total": "", "cart_item_count": ""}
        return None

    def find_elements(self, by=None, value=None):
        self.find_calls.append((by, value))
        if by == By.CSS_SELECTOR and value and ("pdp-add-to-cart-button" in value or "cofr-add-to-basket" in value):
            if not self.real_button:
                return []
            return [self.hidden_button, self.button] if self.hidden_duplicate else [self.button]
        if by == By.XPATH and self.clicked and value and ("Bekijk winkelwagen" in value or "Ik ga bestellen" in value):
            return [self.button]
        if by == By.XPATH and value and "Ik ga bestellen" in value:
            return [self.button]
        return []

    def save_screenshot(self, path):
        self.screenshots.append(path)
        with open(path, "wb") as fh:
            fh.write(b"png")


class _Trace:
    action_id = "watchlist-mediamarkt-test"
    result_status = None

    def __init__(self):
        self.steps = []
        self.results = []

    def step(self, message, details=None, level=None):
        self.steps.append((message, details or {}, level))

    def warning(self, message, details=None, level=None):
        self.steps.append((message, details or {}, level))

    def set_result(self, status, details=None):
        self.result_status = status
        self.results.append((status, details or {}))


def _cfg(tmp_path, **overrides):
    values = {
        "base_dir": tmp_path,
        "queue_check_enabled": True,
        "worker_speed_profile": "fast",
        "worker_click_pause_seconds": 0.0,
        "worker_after_navigation_wait_seconds": 0.0,
        "worker_after_add_to_cart_wait_seconds": 0.0,
        "worker_after_checkout_click_wait_seconds": 0.0,
        "worker_wait_timeout_seconds": 2.0,
        "worker_poll_seconds": 0.05,
        "worker_retry_pause_seconds": 0.0,
        "worker_low_level_debug": False,
        "worker_telegram_trace_level": "normal",
        "worker_telegram_trace_enabled": False,
        "enable_notifications": False,
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _target():
    return ActionTarget(
        site="mediamarkt",
        external_id="1895844",
        title="Pokemon ETB",
        product_url="https://www.mediamarkt.nl/nl/product/_pokemon-1895844.html",
    )


def _job():
    return SeleniumJob(
        site="mediamarkt",
        case="add_to_cart_and_checkout",
        target=_target(),
        action_id="watchlist-mediamarkt-test",
        metadata={"source": "watchlist"},
    )


def _timeline(tmp_path):
    return WorkerActionTimeline(cfg=_cfg(tmp_path), job=_job(), action_id="watchlist-mediamarkt-test")


def test_fast_warm_path_clicks_real_buy_button_and_skips_candidate_dump(tmp_path, monkeypatch):
    driver = _Driver(real_button=True)
    cfg = _cfg(tmp_path)
    trace = _Trace()
    timeline = _timeline(tmp_path)
    dump_calls = []
    queue_calls = []

    monkeypatch.setattr(MediaMarktWorkerCase, "_log_button_candidates", staticmethod(lambda driver, trace=None: dump_calls.append(True)))
    original_fast_queue = MediaMarktWorkerCase._fast_queue_check

    def counted_queue(*args, **kwargs):
        queue_calls.append(kwargs.get("phase"))
        return original_fast_queue(*args, **kwargs)

    monkeypatch.setattr(MediaMarktWorkerCase, "_fast_queue_check", staticmethod(counted_queue))

    MediaMarktWorkerCase.add_to_cart_from_current_page(driver, _target(), cfg, trace, timeline=timeline, job_received_monotonic=timeline.started_monotonic)

    assert driver.button.click_calls == 1
    assert dump_calls == []
    assert queue_calls[0] == "before_warm_add_to_cart"
    assert queue_calls.count("before_warm_add_to_cart") == 1
    before_queue_events = [event for event in timeline.events if event.get("step_name") == "queue_check_before_warm_add_to_cart"]
    assert before_queue_events[-1]["duration_ms"] < 250
    assert any(event["event"] == "mediamarkt_job_received_to_click_timing" for event in timeline.events)


def test_fast_revalidate_ignores_weak_translation_negatives_when_strong_positive_button_exists(tmp_path):
    driver = _Driver(
        real_button=True,
        unavailable_text="Helaas geen bezorging mogelijk. Dit product is binnenkort weer beschikbaar.",
    )
    trace = _Trace()
    timeline = _timeline(tmp_path)

    MediaMarktWorkerCase.add_to_cart_from_current_page(
        driver,
        _target(),
        _cfg(tmp_path),
        trace,
        timeline=timeline,
        job_received_monotonic=timeline.started_monotonic,
    )

    assert driver.button.click_calls == 1
    assert trace.result_status is None
    finished = [event for event in timeline.events if event["event"] == "mediamarkt_fast_revalidate_finished"][-1]
    assert finished["result"] == "success"
    assert finished["availability_decision"]["decision"] == "available"
    assert "helaas geen bezorging mogelijk" in finished["ignored_negative_markers"]


def test_hidden_duplicate_add_to_cart_button_is_ignored(tmp_path):
    driver = _Driver(real_button=True, hidden_duplicate=True)
    timeline = _timeline(tmp_path)

    MediaMarktWorkerCase.add_to_cart_from_current_page(
        driver,
        _target(),
        _cfg(tmp_path),
        _Trace(),
        timeline=timeline,
    )

    assert driver.hidden_button.click_calls == 0
    assert driver.button.click_calls == 1
    finished = [event for event in timeline.events if event["event"] == "mediamarkt_fast_revalidate_finished"][-1]
    assert finished["button_summary"]["visible"] is True
    assert finished["button_summary"]["rect"]["height"] == 48
    assert finished["rejected_buttons"][0]["reason"] == "not_visible"


def test_visible_unavailable_without_positive_or_enabled_button_rejects(tmp_path):
    driver = _Driver(real_button=False, unavailable_text="Niet verkrijgbaar", visible_unavailable=True)
    timeline = _timeline(tmp_path)
    trace = _Trace()

    with pytest.raises(RuntimeError, match="mediamarkt_warm_fast_revalidate_failed"):
        MediaMarktWorkerCase.add_to_cart_from_current_page(
            driver,
            _target(),
            _cfg(tmp_path),
            trace,
            timeline=timeline,
        )

    assert driver.clicked is False
    assert trace.result_status == "mediamarkt_warm_action_stale_stock"
    finished = [event for event in timeline.events if event["event"] == "mediamarkt_fast_revalidate_finished"][-1]
    assert finished["availability_decision"]["decision"] == "unavailable"
    assert finished["availability_decision"]["reason"] == "visible_unavailable_status_without_positive_override"


def test_fast_warm_path_does_not_click_notify_wishlist_store_or_alternatives(tmp_path):
    driver = _Driver(real_button=False, unsafe_button=True)
    timeline = _timeline(tmp_path)

    with pytest.raises(RuntimeError, match="mediamarkt_warm_fast_revalidate_failed"):
        MediaMarktWorkerCase.add_to_cart_from_current_page(driver, _target(), _cfg(tmp_path), _Trace(), timeline=timeline)

    assert driver.clicked is False
    assert driver.button.click_calls == 0
    assert any(event.get("validation_reason") == "buy_button_missing" for event in timeline.events)


def test_checkout_unavailable_banner_fast_aborts_as_out_of_stock(tmp_path, monkeypatch):
    driver = _Driver(
        unavailable_text="Een of meer producten zijn niet langer verkrijgbaar. Jouw winkelwagen is gewijzigd.",
        disabled_checkout=True,
    )
    trace = _Trace()
    timeline = _timeline(tmp_path)
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr("pokemon_parser.workers.mediamarkt_worker.wait_if_queue", lambda *args, **kwargs: None)

    with pytest.raises(MediaMarktCheckoutUnavailableError):
        MediaMarktWorkerCase.checkout(driver, _target(), _cfg(tmp_path), trace, timeline=timeline)

    assert trace.result_status == "out_of_stock_after_cart"
    assert any(event["event"] == "mediamarkt_checkout_unavailable_detected" for event in timeline.events)


def test_worker_action_timeline_orders_events_truncates_and_writes_jsonl(tmp_path):
    timeline = _timeline(tmp_path)
    timeline.record("first", text="x" * 500)
    timeline.record("second", duration_ms=12.3456)

    path = timeline.write(result="failure", reason="test")
    lines = [json.loads(line) for line in open(path, encoding="utf-8")]

    assert [line["event"] for line in lines[:2]] == ["first", "second"]
    assert lines[0]["text"].endswith("chars>")
    assert lines[1]["duration_ms"] == 12.346
    assert len(open(path, encoding="utf-8").read()) < 20000


def test_timed_wait_records_success_and_timeout(tmp_path):
    driver = _Driver()
    timeline = _timeline(tmp_path)

    assert timed_wait(
        timeline=timeline,
        driver=driver,
        wait_name="test_wait_success",
        condition=lambda d: True,
        timeout=0.2,
        selector_info="button#pdp-add-to-cart-button",
        poll_frequency=0.05,
    ) is True

    with pytest.raises(TimeoutException):
        timed_wait(
            timeline=timeline,
            driver=driver,
            wait_name="test_wait_timeout",
            condition=lambda d: False,
            timeout=0.1,
            selector_info="button.missing",
            poll_frequency=0.05,
        )

    assert any(event.get("wait_name") == "test_wait_success" and event.get("result") == "success" for event in timeline.events)
    assert any(event.get("wait_name") == "test_wait_timeout" and event.get("result") == "timeout" for event in timeline.events)


def test_timed_click_records_element_summary_and_urls(tmp_path):
    driver = _Driver()
    timeline = _timeline(tmp_path)

    timed_click(timeline=timeline, driver=driver, click_name="buy", element=driver.button, selector_strategy="#pdp-add-to-cart-button")

    finished = [event for event in timeline.events if event["event"] == "click_finished"][-1]
    assert finished["element"]["id"] == "pdp-add-to-cart-button"
    assert finished["url_before_click"] == driver.current_url
    assert finished["url_after_click"] == driver.current_url
    assert driver.button.click_calls == 1


def test_failure_metadata_includes_low_level_context(tmp_path):
    cfg = _cfg(tmp_path)
    worker = SeleniumWorker(cfg=cfg, job_queue=queue.Queue())
    driver = _Driver(unavailable_text="Helaas zijn deze producten niet verkrijgbaar.", disabled_checkout=True)
    worker.driver = driver
    worker.state.busy = True
    worker._set_action_active("mediamarkt:add_to_cart_and_checkout:1895844")
    timeline = _timeline(tmp_path)
    timeline.record("failed_step", step_name="click", duration_ms=7, selector="#pdp-add-to-cart-button")
    worker.state.last_error = "TimeoutException: button disabled"

    paths = worker._dump_failure_artifacts(_job(), _Trace(), timeline=timeline, total_duration_seconds=1.23)
    metadata = json.loads(open(paths["metadata"], encoding="utf-8").read())

    assert metadata["last_successful_step"] == ""
    assert metadata["timed_out_selector"] == "#pdp-add-to-cart-button"
    assert metadata["current_visible_buttons_summary"]
    assert metadata["current_visible_alert_toast_snackbar_summary"]
    assert metadata["unavailable_markers_found"]["found"] is True
    assert metadata["active_worker_action_lock_state"]["active_worker_action"]
    assert metadata["timeline_path"] == paths["timeline"]
    assert metadata["sensitive_artifacts_omitted"] is True
    assert "screenshot" not in paths
    assert "html" not in paths


def test_warm_refresh_configure_skips_when_action_active(tmp_path):
    logs = []
    storage = SimpleNamespace(insert_runtime_log=lambda **kwargs: logs.append(kwargs))
    worker = SeleniumWorker(cfg=_cfg(tmp_path, action_mode="selenium", watchlist_warm_tabs_enabled=True, selenium_keep_browser_alive=True, watchlist_warm_tabs_max=6), job_queue=queue.Queue(), storage=storage)
    worker._set_action_active("mediamarkt:add_to_cart_and_checkout:1895844")
    worker.state.busy = True

    result = worker.configure_warm_tabs([{"site": "mediamarkt", "article_number": "1895844", "url": "https://example.test/1895844", "title": "ETB", "enabled": True}])

    assert result["ok"] is False
    messages = [log["message"] for log in logs]
    assert "warm_refresh_skipped_worker_busy" in messages
    assert "warm_refresh_attempted_while_action_active" in messages
