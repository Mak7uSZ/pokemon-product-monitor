import asyncio
import queue
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import pokemon_parser.api.routes.settings as settings_routes
from pokemon_parser.api.services.runtime_manager import RuntimeManager
from pokemon_parser.api.services.watchlist_manager import WatchlistManager
from pokemon_parser.config import load_config
from pokemon_parser.engine.access_control import SourceAccessController
from pokemon_parser.engine.selenium_dispatcher import SeleniumDispatcher
from pokemon_parser.models import ActionTarget, SeleniumJob
from pokemon_parser.workers.mediamarkt_worker import MediaMarktWorkerCase
from pokemon_parser.workers.selenium_worker import ChallengeBlockedError, SeleniumWorker


class _FakeWorker:
    def __init__(self):
        self.start_calls = 0
        self.shutdown_calls = 0
        self.join_calls = 0
        self.driver = None
        self.state = SimpleNamespace(
            started=False,
            ready=False,
            busy=False,
            last_error="",
            last_job="",
            last_result="",
            last_duration_seconds=0.0,
            jobs_completed=0,
            jobs_failed=0,
            jobs_timed_out=0,
            driver_rebuilds=0,
            last_diagnostic_snapshot="",
            driver_session_id="",
            browser_started_at=None,
            last_driver_create_at=None,
            last_driver_quit_at=None,
            chromedriver_pid=None,
            chrome_pid=None,
            tracked_chrome_pids=[],
            orphan_app_chrome_pids=[],
            selenium_window_count=None,
            selenium_top_level_window_ids=[],
            selenium_top_level_window_count=None,
            selenium_top_level_window_id_by_handle={},
            window_handles_count=0,
            window_handles_current_urls={},
            last_window_snapshot_at=None,
            last_start_at=None,
            last_stop_at=None,
            lifecycle_state="stopped",
            config={},
            prewarmed=False,
            last_prewarm_skip_reason="",
            last_prewarm_error="",
            warm_tabs_enabled=False,
            warm_tabs_count=0,
            warm_tabs_max=0,
            warm_tab_urls=[],
            active_action="",
            active_worker_action="",
            warm_refresh_running=False,
            warm_refresh_paused_reason="",
            last_action_latency_seconds=0.0,
            last_button_search_latency_seconds=0.0,
            duplicate_start_ignored_count=0,
            duplicate_start_guard_count=0,
        )
        self._alive = False

    def start(self):
        self.start_calls += 1
        self._alive = True
        self.state.lifecycle_state = "running"
        return True

    def is_alive(self):
        return self._alive

    def shutdown(self):
        self.shutdown_calls += 1
        self._alive = False

    def join(self, timeout=None):
        self.join_calls += 1


class _FakeDriver:
    session_id = "session-1"
    capabilities = {"goog:processID": 456}

    def __init__(self):
        self.service = SimpleNamespace(process=SimpleNamespace(pid=123))
        self.quit_calls = 0

    def quit(self):
        self.quit_calls += 1


class _FakeSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def new_window(self, kind):
        handle = f"tab-{len(self.driver.window_handles)}"
        self.driver.window_handles.append(handle)
        self.driver.urls[handle] = "about:blank"
        self.driver.top_window_ids[handle] = self.driver.top_window_ids.get(self.driver.current_window_handle, 1)
        self.driver.current_window_handle = handle
        self.driver.switch_history.append(("new_window", kind, handle))

    def window(self, handle):
        if handle not in self.driver.window_handles:
            raise RuntimeError(f"unknown handle: {handle}")
        self.driver.current_window_handle = handle
        self.driver.switch_history.append(("window", handle))


class _FakeElement:
    tag_name = "button"

    def __init__(self, *, text="Ik wil bestellen", element_id="pdp-add-to-cart-button", data_test="cofr-add-to-basket-button"):
        self.text = text
        self.element_id = element_id
        self.data_test = data_test
        self.click_calls = 0
        self.rect = {"x": 10, "y": 20, "width": 180, "height": 44}

    def get_attribute(self, name):
        return {
            "id": self.element_id,
            "data-test": self.data_test,
            "aria-label": self.text,
            "class": "buy-button",
            "href": "",
            "disabled": "",
            "aria-disabled": "false",
        }.get(name, "")

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.click_calls += 1


class _FakeWarmDriver:
    session_id = "warm-session-1"
    capabilities = {"goog:processID": 654}

    def __init__(self, *, dom_status="buyable"):
        self.service = SimpleNamespace(process=SimpleNamespace(pid=321))
        self.window_handles = ["main"]
        self.current_window_handle = "main"
        self.urls = {"main": "about:blank"}
        self.top_window_ids = {"main": 1}
        self.switch_to = _FakeSwitchTo(self)
        self.dom_status = dom_status
        self.get_calls = []
        self.refresh_calls = 0
        self.quit_calls = 0
        self.close_calls = []
        self.page_load_timeouts = []
        self.switch_history = []
        self.buy_button = _FakeElement()
        self.execute_script_calls = []
        self.challenge_pages = {}

    @property
    def current_url(self):
        return self.urls.get(self.current_window_handle, "about:blank")

    def get(self, url):
        self.urls[self.current_window_handle] = url
        self.get_calls.append((self.current_window_handle, url))

    def refresh(self):
        self.refresh_calls += 1

    def execute_script(self, script, *args):
        self.execute_script_calls.append((script, args))
        if "challengeSignalSnapshot" in script:
            return self.challenge_pages.get(
                self.current_url,
                {
                    "title": "Product page",
                    "url": self.current_url,
                    "headings": "Pokemon product",
                    "bodyText": "Product details and availability",
                    "selectorSignals": [],
                },
            )
        if "document.readyState" in script:
            return "complete"
        if "scrollIntoView" in script:
            return None
        if "arguments[0].click" in script and args:
            args[0].click()
            return None
        if "rejectionPhrases" in script or "cofr-add-to-basket" in script:
            ok = self.dom_status == "buyable"
            return {
                "ok": ok,
                "reason": "ready" if ok else "visible_unavailable_status_without_positive_override",
                "button": self.buy_button if ok else None,
                "button_summary": {
                    "tag": "button",
                    "text": "Ik wil bestellen",
                    "id": "pdp-add-to-cart-button",
                    "data_test": "cofr-add-to-basket-button",
                    "aria_label": "Ik wil bestellen",
                    "visible": True,
                    "enabled": True,
                    "disabled_attr": "",
                    "aria_disabled": "false",
                    "rect": {"x": 10, "y": 20, "width": 180, "height": 44},
                },
                "availability_markers": ["data-product-online-status=AVAILABLE"],
                "rejection_markers": [] if ok else ["niet verkrijgbaar"],
                "rejected_buttons": [],
                "availability_decision": {
                    "decision": "available" if ok else "unavailable",
                    "reason": (
                        "strong_positive_available_delivery_and_visible_enabled_button"
                        if ok
                        else "visible_unavailable_status_without_positive_override"
                    ),
                    "positive_markers": ["data-product-online-status=AVAILABLE"] if ok else [],
                    "rejection_markers": [] if ok else ["niet verkrijgbaar"],
                    "ignored_negative_markers": [],
                },
            }
        if "document.querySelectorAll('button,a[role=\"button\"],a[href]')" in script:
            return []
        if "const selectors = [" in script:
            return []
        return None

    def find_elements(self, by=None, value=None):
        if value and ("pdp-add-to-cart-button" in value or "cofr-add-to-basket" in value):
            return [self.buy_button] if self.dom_status == "buyable" else []
        return []

    def set_page_load_timeout(self, timeout):
        self.page_load_timeouts.append(timeout)

    def execute_cdp_cmd(self, command, params):
        if command == "Browser.getWindowForTarget":
            return {"windowId": self.top_window_ids.get(self.current_window_handle, 1)}
        if command == "Target.createTarget":
            handle = params.get("targetId") or f"cdp-{len(self.window_handles)}"
            self.window_handles.append(handle)
            self.urls[handle] = params.get("url", "about:blank")
            self.top_window_ids[handle] = 1
            return {"targetId": handle}
        return {}

    def close(self):
        handle = self.current_window_handle
        self.close_calls.append(handle)
        if handle in self.window_handles:
            self.window_handles.remove(handle)
        self.urls.pop(handle, None)
        self.top_window_ids.pop(handle, None)
        self.current_window_handle = self.window_handles[0] if self.window_handles else ""

    def quit(self):
        self.quit_calls += 1


class _Trace:
    def __init__(self):
        self.steps = []
        self.result_status = None
        self.result_details = None

    def step(self, message, details=None, level=None):
        self.steps.append((message, details or {}, level))

    def set_result(self, status, details=None):
        self.result_status = status
        self.result_details = details or {}


def _selenium_job() -> SeleniumJob:
    return SeleniumJob(
        site="mediamarkt",
        case="add_to_cart",
        target=ActionTarget(
            site="mediamarkt",
            external_id="mm-1",
            title="Pokemon Booster",
            product_url="https://example.test/mm-1",
        ),
        action_id="action-1",
    )


def _warm_cfg(tmp_path, **overrides):
    values = {
        "base_dir": tmp_path,
        "action_mode": "selenium",
        "selenium_prewarm": True,
        "selenium_prewarm_enabled": True,
        "selenium_prewarm_on_runtime_start": True,
        "selenium_keep_browser_alive": True,
        "watchlist_warm_tabs_enabled": True,
        "watchlist_warm_tabs_max": 6,
        "watchlist_warm_tab_refresh_interval_seconds": 30.0,
        "watchlist_warm_tab_min_refresh_interval_seconds": 15.0,
        "watchlist_warm_tab_reload_timeout_seconds": 8.0,
        "watchlist_warm_tab_stale_after_seconds": 60.0,
        "challenge_cooldown_base_seconds": 30.0,
        "challenge_cooldown_multiplier": 2.0,
        "challenge_cooldown_max_seconds": 900.0,
        "challenge_cooldown_jitter_ratio": 0.0,
        "mediamarkt_warm_tabs_enabled": True,
        "mediamarkt_fast_action_refresh_policy": "never_if_warm_recent",
        "mediamarkt_warm_recent_threshold_seconds": 2.0,
        "queue_check_enabled": True,
        "queue_wait_timeout_seconds": 300.0,
        "queue_poll_seconds": 1.0,
        "worker_wait_timeout_seconds": 20.0,
        "worker_low_level_debug": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _warm_item(external_id="1895844", *, url=None, site="mediamarkt", pinned=False):
    return {
        "site": site,
        "article_number": external_id,
        "product_key": external_id,
        "title": f"Product {external_id}",
        "url": url or f"https://www.mediamarkt.nl/nl/product/_pokemon-{external_id}.html",
        "enabled": True,
        "pinned": pinned,
    }


def _mediamarkt_job(external_id="1895844") -> SeleniumJob:
    return SeleniumJob(
        site="mediamarkt",
        case="add_to_cart",
        target=ActionTarget(
            site="mediamarkt",
            external_id=external_id,
            title=f"Product {external_id}",
            product_url=f"https://www.mediamarkt.nl/nl/product/_pokemon-{external_id}.html",
        ),
        action_id=f"action-{external_id}",
    )


def test_selenium_dispatcher_start_is_idempotent(caplog):
    worker = _FakeWorker()
    dispatcher = SeleniumDispatcher(queue.Queue(), worker_factory=lambda: worker)

    caplog.set_level("INFO")
    assert dispatcher.start() is True
    assert dispatcher.start() is False
    assert worker.start_calls == 1
    snapshot = dispatcher.lifecycle_snapshot()
    assert snapshot["worker_thread_alive"] is True
    assert snapshot["lifecycle_state"] == "running"
    assert snapshot["duplicate_start_ignored_count"] == 1
    assert snapshot["duplicate_start_guard_count"] == 1
    assert "selenium_dispatcher_start_ignored_already_running" in caplog.text

    assert dispatcher.stop(timeout=0.01) is True
    assert worker.shutdown_calls == 1


def test_selenium_dispatcher_starts_worker_once_for_queued_jobs():
    worker = _FakeWorker()
    created = []
    dispatcher = SeleniumDispatcher(
        queue.Queue(),
        worker_factory=lambda: created.append(worker) or worker,
    )

    assert dispatcher.submit(_selenium_job()).status == "queued"
    assert dispatcher.submit(_selenium_job()).status == "already_queued"

    assert len(created) == 1
    assert worker.start_calls == 1
    assert dispatcher.counts()["pending"] == 1
    dispatcher.stop(timeout=0.01)


def test_selenium_worker_start_is_idempotent(tmp_path, caplog):
    worker = SeleniumWorker(
        cfg=_warm_cfg(tmp_path),
        job_queue=queue.Queue(),
    )

    caplog.set_level("INFO")
    try:
        assert worker.start() is True
        assert worker.start() is False
    finally:
        worker.shutdown()
        worker.join(timeout=1)

    assert worker.state.duplicate_start_ignored_count == 1
    assert worker.state.duplicate_start_guard_count == 1
    assert "selenium_worker_start_ignored_already_running" in caplog.text


def test_selenium_worker_shutdown_quits_existing_driver(tmp_path):
    worker = SeleniumWorker(
        cfg=SimpleNamespace(base_dir=tmp_path),
        job_queue=queue.Queue(),
    )
    driver = _FakeDriver()
    worker.driver = driver
    worker._record_driver_started(driver)

    worker.shutdown()

    assert driver.quit_calls == 1
    assert worker.driver is None
    assert worker.state.driver_session_id == ""
    assert worker.state.chromedriver_pid is None
    assert worker.state.chrome_pid is None


def test_selenium_worker_prewarm_reuses_one_driver(tmp_path, caplog):
    worker = SeleniumWorker(
        cfg=_warm_cfg(tmp_path),
        job_queue=queue.Queue(),
    )
    driver = _FakeWarmDriver()
    init_calls = []

    def fake_init_driver():
        init_calls.append(time.monotonic())
        return driver

    worker.init_driver = fake_init_driver

    caplog.set_level("INFO")
    assert worker.ensure_driver() is driver
    assert worker.ensure_driver() is driver
    assert len(init_calls) == 1
    assert worker.driver is driver
    assert "selenium_driver_create_skipped_existing_driver" in caplog.text


def test_dispatcher_prewarm_browser_creates_driver_once(tmp_path):
    created = []
    driver = _FakeWarmDriver()

    def worker_factory():
        worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
        worker.init_driver = lambda: created.append(driver) or driver
        return worker

    dispatcher = SeleniumDispatcher(queue.Queue(), worker_factory=worker_factory)
    try:
        first = dispatcher.prewarm_browser(reason="test", wait_ready_timeout=2)
        second = dispatcher.prewarm_browser(reason="test", wait_ready_timeout=2)
        snapshot = dispatcher.lifecycle_snapshot()

        assert first["ok"] is True
        assert second["ok"] is True
        assert second["reason"] == "driver_already_exists"
        assert len(created) == 1
        assert snapshot["driver_exists"] is True
        assert snapshot["prewarmed"] is True
        assert snapshot["duplicate_start_ignored_count"] >= 1
    finally:
        dispatcher.stop(timeout=1)


def test_prewarm_disabled_skips_driver_but_lazy_ensure_still_creates_driver(tmp_path):
    worker = SeleniumWorker(
        cfg=_warm_cfg(tmp_path, selenium_prewarm_enabled=False),
        job_queue=queue.Queue(),
    )
    driver = _FakeWarmDriver()
    init_calls = []
    worker.init_driver = lambda: init_calls.append(driver) or driver

    result = worker.prewarm_browser(reason="test")

    assert result == {"ok": False, "skipped": True, "reason": "prewarm_disabled"}
    assert worker.driver is None
    assert worker.ensure_driver() is driver
    assert init_calls == [driver]


def test_warm_tabs_open_as_tabs_not_extra_browsers(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: driver.dom_status),
    )
    worker = SeleniumWorker(
        cfg=_warm_cfg(tmp_path),
        job_queue=queue.Queue(),
    )
    driver = _FakeWarmDriver(dom_status="buyable")
    worker.driver = driver
    worker.init_driver = lambda: pytest.fail("warm tabs must reuse the existing driver")

    configured = worker.configure_warm_tabs([_warm_item("1895844"), _warm_item("1900000")])
    result = worker.preload_warm_tabs_now()

    assert configured == {"ok": True, "configured": 2}
    assert result == {"ok": True, "opened": 2}
    assert driver.window_handles == ["main", "tab-1"]
    assert len(driver.get_calls) == 2
    assert driver.get_calls[0] == ("main", "https://www.mediamarkt.nl/nl/product/_pokemon-1895844.html")
    assert driver.get_calls[1] == ("tab-1", "https://www.mediamarkt.nl/nl/product/_pokemon-1900000.html")
    assert worker.state.warm_tabs_count == 2
    assert worker.state.window_handles_count == 2
    assert worker.state.selenium_window_count == 1
    assert all(tab["warm_state"] == "ready" for tab in worker.warm_tabs_snapshot())


def test_initial_blank_window_is_reused_for_first_warm_watchlist_product(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: driver.dom_status),
    )
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    driver = _FakeWarmDriver(dom_status="buyable")
    worker.driver = driver

    worker.configure_warm_tabs([_warm_item("1895844")])
    result = worker.preload_warm_tabs_now()

    assert result == {"ok": True, "opened": 1}
    assert driver.window_handles == ["main"]
    assert driver.urls["main"] == "https://www.mediamarkt.nl/nl/product/_pokemon-1895844.html"
    assert not any(entry[0] == "new_window" for entry in driver.switch_history)


def test_new_window_second_top_level_is_closed_and_cdp_fallback_reuses_main_window(tmp_path, monkeypatch):
    class _SecondWindowSwitchTo(_FakeSwitchTo):
        def new_window(self, kind):
            super().new_window(kind)
            self.driver.top_window_ids[self.driver.current_window_handle] = 99

    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: driver.dom_status),
    )
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    driver = _FakeWarmDriver(dom_status="buyable")
    driver.urls["main"] = "https://already-open.test/"
    driver.switch_to = _SecondWindowSwitchTo(driver)
    worker.driver = driver

    worker.configure_warm_tabs([_warm_item("1895844")])
    result = worker.preload_warm_tabs_now()

    assert result == {"ok": True, "opened": 1}
    assert driver.close_calls == ["tab-1"]
    assert driver.window_handles == ["main", "cdp-1"]
    assert driver.urls["cdp-1"] == "https://www.mediamarkt.nl/nl/product/_pokemon-1895844.html"
    assert set(driver.top_window_ids.values()) == {1}
    assert worker.state.selenium_top_level_window_count == 1


def test_warm_tab_open_does_not_fallback_to_window_open(tmp_path, monkeypatch):
    class _FailingSwitchTo(_FakeSwitchTo):
        def new_window(self, kind):
            raise RuntimeError("new tab blocked")

    class _NoWindowOpenDriver(_FakeWarmDriver):
        def __init__(self):
            super().__init__(dom_status="buyable")
            self.switch_to = _FailingSwitchTo(self)

        def execute_script(self, script):
            pytest.fail("warm tabs must not use window.open fallback")

    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: driver.dom_status),
    )
    worker = SeleniumWorker(
        cfg=_warm_cfg(tmp_path),
        job_queue=queue.Queue(),
    )
    driver = _NoWindowOpenDriver()
    driver.urls["main"] = "https://already-open.test/"
    worker.driver = driver

    worker.configure_warm_tabs([_warm_item("1895844")])
    result = worker.preload_warm_tabs_now()

    assert result == {"ok": True, "opened": 0}
    assert driver.window_handles == ["main"]
    assert worker.warm_tabs_snapshot()[0]["warm_state"] == "failed"


def test_watchlist_action_uses_existing_warm_tab_without_driver_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: driver.dom_status),
    )
    worker = SeleniumWorker(
        cfg=_warm_cfg(tmp_path),
        job_queue=queue.Queue(),
    )
    driver = _FakeWarmDriver(dom_status="buyable")
    worker.driver = driver
    worker.init_driver = lambda: pytest.fail("warm action must not start a new driver")
    worker.configure_warm_tabs([_warm_item("1895844")])
    worker.preload_warm_tabs_now()

    trace = _Trace()
    used_warm_tab = worker._prepare_mediamarkt_warm_action(_mediamarkt_job("1895844"), trace, time.monotonic())

    assert used_warm_tab is True
    assert any(entry == ("window", "main") for entry in driver.switch_history)
    assert driver.refresh_calls == 0
    assert any(step[0] == "watchlist_action_using_warm_tab" for step in trace.steps)


def test_watchlist_action_refreshes_stale_warm_tab_only_when_policy_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: driver.dom_status),
    )
    worker = SeleniumWorker(
        cfg=_warm_cfg(tmp_path, mediamarkt_warm_recent_threshold_seconds=2.0),
        job_queue=queue.Queue(),
    )
    driver = _FakeWarmDriver(dom_status="buyable")
    worker.driver = driver
    worker.configure_warm_tabs([_warm_item("1895844")])
    worker.preload_warm_tabs_now()
    with worker._warm_tabs_lock:
        tab = next(iter(worker._warm_tabs.values()))
        tab.last_refreshed_epoch = time.time() - 10
        tab.last_loaded_epoch = time.time() - 10

    assert worker._prepare_mediamarkt_warm_action(_mediamarkt_job("1895844"), _Trace(), time.monotonic()) is True
    assert driver.refresh_calls == 0

    worker_micro = SeleniumWorker(
        cfg=_warm_cfg(tmp_path, mediamarkt_fast_action_refresh_policy="micro_revalidate_only", mediamarkt_warm_recent_threshold_seconds=2.0),
        job_queue=queue.Queue(),
    )
    driver_micro = _FakeWarmDriver(dom_status="buyable")
    worker_micro.driver = driver_micro
    worker_micro.configure_warm_tabs([_warm_item("1895844")])
    worker_micro.preload_warm_tabs_now()
    with worker_micro._warm_tabs_lock:
        tab = next(iter(worker_micro._warm_tabs.values()))
        tab.last_refreshed_epoch = time.time() - 10
        tab.last_loaded_epoch = time.time() - 10

    assert worker_micro._prepare_mediamarkt_warm_action(_mediamarkt_job("1895844"), _Trace(), time.monotonic()) is True
    assert driver_micro.refresh_calls == 0


def test_watchlist_action_missing_warm_tab_falls_back_to_generic_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: driver.dom_status),
    )
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    driver = _FakeWarmDriver(dom_status="buyable")
    worker.driver = driver
    worker.init_driver = lambda: pytest.fail("missing warm action must not create a warm tab in the hot path")

    trace = _Trace()
    used_warm_tab = worker._prepare_mediamarkt_warm_action(_mediamarkt_job("1895844"), trace, time.monotonic())

    assert used_warm_tab is False
    assert driver.window_handles == ["main"]
    assert driver.urls["main"] == "about:blank"
    assert worker.warm_tabs_snapshot() == []


def test_warm_refresh_pauses_during_active_action(tmp_path, monkeypatch):
    logs = []
    storage = SimpleNamespace(insert_runtime_log=lambda **kwargs: logs.append(kwargs))
    worker = SeleniumWorker(
        cfg=_warm_cfg(
            tmp_path,
            watchlist_warm_tab_min_refresh_interval_seconds=0.0,
            watchlist_warm_tab_refresh_interval_seconds=0.0,
        ),
        job_queue=queue.Queue(),
        storage=storage,
    )
    worker.configure_warm_tabs([_warm_item("1895844")])
    with worker._warm_tabs_lock:
        tab = next(iter(worker._warm_tabs.values()))
        tab.window_handle = "tab-1"
        tab.last_loaded_epoch = time.time() - 120
        tab.last_refreshed_epoch = time.time() - 120
    worker.state.busy = True
    load_calls = []
    monkeypatch.setattr(worker, "_load_warm_tab", lambda tab, reason: load_calls.append((tab.external_id, reason)))

    worker._warm_tabs_idle_tick()

    assert load_calls == []
    assert any(log["message"] == "warm_refresh_skipped_worker_busy" for log in logs)
    assert worker.state.warm_refresh_paused_reason == "state_busy"


def test_warm_refresh_skips_when_action_lock_is_held(tmp_path, monkeypatch):
    logs = []
    storage = SimpleNamespace(insert_runtime_log=lambda **kwargs: logs.append(kwargs))
    worker = SeleniumWorker(
        cfg=_warm_cfg(
            tmp_path,
            watchlist_warm_tab_min_refresh_interval_seconds=0.0,
            watchlist_warm_tab_refresh_interval_seconds=0.0,
        ),
        job_queue=queue.Queue(),
        storage=storage,
    )
    worker.configure_warm_tabs([_warm_item("1895844")])
    with worker._warm_tabs_lock:
        tab = next(iter(worker._warm_tabs.values()))
        tab.window_handle = "tab-1"
        tab.last_loaded_epoch = time.time() - 120
        tab.last_refreshed_epoch = time.time() - 120
    load_calls = []
    monkeypatch.setattr(worker, "_load_warm_tab", lambda tab, reason: load_calls.append((tab.external_id, reason)))

    worker._action_lock.acquire()
    try:
        worker._warm_tabs_idle_tick()
    finally:
        worker._action_lock.release()

    assert load_calls == []
    assert any(log["message"] == "warm_refresh_skipped_worker_busy" for log in logs)
    assert worker.state.warm_refresh_paused_reason == "action_lock_busy"


def test_lifecycle_snapshot_does_not_touch_driver_during_active_action(tmp_path, monkeypatch):
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    worker.driver = _FakeWarmDriver()
    worker.state.busy = True
    worker._set_action_active("mediamarkt:add_to_cart_and_checkout:1895844")
    snapshot_calls = []

    monkeypatch.setattr(worker, "_snapshot_window_handles_locked", lambda reason: snapshot_calls.append(reason))

    worker.refresh_lifecycle_snapshot()

    assert snapshot_calls == []
    assert worker.state.active_worker_action == "mediamarkt:add_to_cart_and_checkout:1895844"


def test_warm_tab_out_of_stock_dom_prevents_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: driver.dom_status),
    )
    worker = SeleniumWorker(
        cfg=_warm_cfg(tmp_path),
        job_queue=queue.Queue(),
    )
    driver = _FakeWarmDriver(dom_status="out_of_stock")
    worker.driver = driver
    worker.init_driver = lambda: pytest.fail("warm action must reuse the existing driver")
    worker.configure_warm_tabs([_warm_item("1895844")])
    worker.preload_warm_tabs_now()
    trace = _Trace()

    with pytest.raises(RuntimeError, match="mediamarkt_warm_action_stale_stock"):
        worker._prepare_mediamarkt_warm_action(_mediamarkt_job("1895844"), trace, time.monotonic())

    assert trace.result_status == "mediamarkt_warm_action_stale_stock"
    assert trace.result_details["dom_status"] == "out_of_stock"


@pytest.mark.parametrize(
    "signals,expected_type",
    [
        ({"title": "Are you a bot?", "bodyText": "", "selectorSignals": []}, "bot_check"),
        (
            {
                "title": "Security check",
                "bodyText": "Complete the check",
                "selectorSignals": ["recaptcha_iframe"],
            },
            "captcha",
        ),
        ({"title": "Access denied", "bodyText": "Request blocked", "selectorSignals": []}, "access_denied"),
    ],
)
def test_warm_tab_challenge_is_quarantined_and_action_is_blocked(
    tmp_path,
    monkeypatch,
    signals,
    expected_type,
):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: pytest.fail("challenge must stop before product DOM extraction")),
    )
    queued = object()
    job_queue = queue.Queue()
    job_queue.put(queued)
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=job_queue)
    driver = _FakeWarmDriver()
    target_url = _warm_item("1895844")["url"]
    driver.challenge_pages[target_url] = {"url": target_url, "headings": "", **signals}
    worker.driver = driver
    worker.configure_warm_tabs([_warm_item("1895844")])

    result = worker.preload_warm_tabs_now()
    snapshot = worker.warm_tabs_snapshot()[0]

    assert result == {"ok": True, "opened": 0}
    assert snapshot["warm_state"] == "quarantined"
    assert snapshot["challenge_type"] == expected_type
    assert snapshot["challenge_reason_code"].startswith("challenge_")
    if expected_type == "captcha":
        assert snapshot["access_state"] == "manually_blocked"
        assert snapshot["manual_action_required"] is True
    assert worker.state.challenge_sources["mediamarkt"]["state"] == "cooling_down"
    assert job_queue.qsize() == 1
    assert job_queue.get_nowait() is queued

    trace = _Trace()
    with pytest.raises(ChallengeBlockedError, match="challenge_blocked"):
        worker._prepare_mediamarkt_warm_action(_mediamarkt_job("1895844"), trace, time.monotonic())
    assert trace.result_status == "challenge_blocked"
    assert driver.buy_button.click_calls == 0


@pytest.mark.parametrize(
    "title,body",
    [
        ("Product not found", "404 - this product does not exist"),
        ("Server error", "500 - the product service is temporarily unavailable"),
    ],
)
def test_ordinary_error_page_is_not_misclassified_as_challenge(tmp_path, monkeypatch, title, body):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: "unknown"),
    )
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    driver = _FakeWarmDriver(dom_status="unknown")
    target_url = _warm_item("1895844")["url"]
    driver.challenge_pages[target_url] = {
        "title": title,
        "url": target_url,
        "headings": title,
        "bodyText": body,
        "selectorSignals": [],
    }
    worker.driver = driver
    worker.configure_warm_tabs([_warm_item("1895844")])

    result = worker.preload_warm_tabs_now()
    snapshot = worker.warm_tabs_snapshot()[0]

    assert result == {"ok": True, "opened": 1}
    assert snapshot["warm_state"] == "ready"
    assert snapshot["challenge_type"] == ""


def test_one_challenged_tab_does_not_pause_healthy_sibling(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: "buyable"),
    )
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    driver = _FakeWarmDriver()
    first = _warm_item("1895844")
    second = _warm_item("1900000")
    driver.challenge_pages[first["url"]] = {
        "title": "Verify you are human",
        "url": first["url"],
        "headings": "Verify you are human",
        "bodyText": "",
        "selectorSignals": [],
    }
    worker.driver = driver
    worker.configure_warm_tabs([first, second])

    result = worker.preload_warm_tabs_now()
    snapshots = {tab["external_id"]: tab for tab in worker.warm_tabs_snapshot()}

    assert result == {"ok": True, "opened": 1}
    assert snapshots["1895844"]["warm_state"] == "quarantined"
    assert snapshots["1900000"]["warm_state"] == "ready"
    assert worker.state.challenge_sources["mediamarkt"]["tabs_challenged"] == 1
    assert worker.state.challenge_sources["mediamarkt"]["state"] == "normal"


def test_all_source_tabs_challenged_pauses_only_that_source(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: "buyable"),
    )
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    driver = _FakeWarmDriver()
    first = _warm_item("1895844")
    second = _warm_item("1900000")
    bol = _warm_item("9300000", site="bol", pinned=True, url="https://www.bol.com/p/9300000")
    for item in (first, second):
        driver.challenge_pages[item["url"]] = {
            "title": "Access denied",
            "url": item["url"],
            "headings": "Access denied",
            "bodyText": "Request blocked",
            "selectorSignals": [],
        }
    worker.driver = driver
    worker.configure_warm_tabs([first, second, bol])

    result = worker.preload_warm_tabs_now()
    snapshots = {tab["external_id"]: tab for tab in worker.warm_tabs_snapshot()}

    assert result == {"ok": True, "opened": 1}
    assert snapshots["1895844"]["warm_state"] == "quarantined"
    assert snapshots["1900000"]["warm_state"] == "quarantined"
    assert snapshots["9300000"]["warm_state"] == "ready"
    assert worker.state.challenge_sources["mediamarkt"]["state"] == "cooling_down"
    assert worker.state.challenge_sources["bol"]["state"] == "normal"


def test_challenge_cooldown_grows_to_cap_and_controlled_probe_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: "buyable"),
    )
    now = [100.0]
    worker = SeleniumWorker(
        cfg=_warm_cfg(
            tmp_path,
            challenge_cooldown_base_seconds=10.0,
            challenge_cooldown_multiplier=2.0,
            challenge_cooldown_max_seconds=15.0,
        ),
        job_queue=queue.Queue(),
    )
    worker._challenge_access = SourceAccessController(clock=lambda: now[0], random_fn=lambda: 0.0)
    driver = _FakeWarmDriver()
    item = _warm_item("1895844")
    challenge_page = {
        "title": "Verify you are human",
        "url": item["url"],
        "headings": "Verify you are human",
        "bodyText": "",
        "selectorSignals": [],
    }
    driver.challenge_pages[item["url"]] = challenge_page
    worker.driver = driver
    worker.configure_warm_tabs([item])
    worker.preload_warm_tabs_now()
    first_retry = worker.warm_tabs_snapshot()[0]["retry_after_epoch"]
    assert first_retry == 110.0

    now[0] = 111.0
    with worker._warm_tabs_lock:
        tab = next(iter(worker._warm_tabs.values()))
    worker._load_warm_tab(tab, reason="refresh")
    second_retry = worker.warm_tabs_snapshot()[0]["retry_after_epoch"]
    assert second_retry == 126.0

    now[0] = 127.0
    driver.challenge_pages.clear()
    worker._load_warm_tab(tab, reason="refresh")
    recovered = worker.warm_tabs_snapshot()[0]

    assert recovered["warm_state"] == "ready"
    assert recovered["challenge_reason_code"] == ""
    assert recovered["access_state"] == "recovered"
    assert worker.state.challenge_sources["mediamarkt"]["state"] == "recovered"


def test_challenge_does_not_trigger_browser_restart_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    driver = _FakeWarmDriver()
    item = _warm_item("1895844")
    driver.challenge_pages[item["url"]] = {
        "title": "Are you a bot?",
        "url": item["url"],
        "headings": "Are you a bot?",
        "bodyText": "",
        "selectorSignals": [],
    }
    worker.driver = driver
    worker.configure_warm_tabs([item])
    worker.rebuild_driver = lambda: pytest.fail("challenge handling must not restart the browser automatically")

    worker.preload_warm_tabs_now()
    trace = _Trace()
    with pytest.raises(ChallengeBlockedError):
        worker._prepare_mediamarkt_warm_action(_mediamarkt_job("1895844"), trace, time.monotonic())

    assert worker.state.driver_rebuilds == 0


def test_challenge_appearing_after_warm_load_is_caught_before_action(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: "buyable"),
    )
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    driver = _FakeWarmDriver()
    item = _warm_item("1895844")
    worker.driver = driver
    worker.configure_warm_tabs([item])
    assert worker.preload_warm_tabs_now() == {"ok": True, "opened": 1}

    driver.challenge_pages[item["url"]] = {
        "title": "Security check",
        "url": item["url"],
        "headings": "Verify you are human",
        "bodyText": "",
        "selectorSignals": ["hcaptcha_iframe"],
    }
    trace = _Trace()

    with pytest.raises(ChallengeBlockedError):
        worker._prepare_mediamarkt_warm_action(_mediamarkt_job("1895844"), trace, time.monotonic())

    assert trace.result_status == "challenge_blocked"
    assert driver.buy_button.click_calls == 0
    assert worker.warm_tabs_snapshot()[0]["challenge_type"] == "captcha"


def test_worker_does_not_hide_browser_automation_fingerprints():
    source = Path(__file__).resolve().parents[1] / "src" / "pokemon_parser" / "workers" / "selenium_worker.py"
    worker_source = source.read_text(encoding="utf-8")

    assert "--disable-blink-features=AutomationControlled" not in worker_source
    assert 'excludeSwitches", ["enable-automation"]' not in worker_source
    assert '"useAutomationExtension", False' not in worker_source


def test_stop_runtime_closes_one_driver_and_marks_warm_tabs_stale(tmp_path):
    worker = SeleniumWorker(
        cfg=_warm_cfg(tmp_path),
        job_queue=queue.Queue(),
    )
    driver = _FakeWarmDriver()
    worker.driver = driver
    worker.configure_warm_tabs([_warm_item("1895844")])
    with worker._warm_tabs_lock:
        tab = next(iter(worker._warm_tabs.values()))
        tab.window_handle = "tab-1"
        tab.warm_state = "ready"

    worker.shutdown()

    assert driver.quit_calls == 1
    assert worker.driver is None
    snapshot = worker.warm_tabs_snapshot()
    assert snapshot[0]["window_handle"] == ""
    assert snapshot[0]["warm_state"] == "stale"


def test_rebuild_driver_quits_existing_driver_before_creating_new_driver(tmp_path, monkeypatch):
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    old_driver = _FakeWarmDriver()
    new_driver = _FakeWarmDriver()
    worker.driver = old_driver
    events = []

    def fake_close_driver(*, reason):
        events.append(("quit", reason))
        worker.driver = None

    def fake_init_driver():
        events.append(("create", "new_driver"))
        return new_driver

    monkeypatch.setattr(worker, "close_driver", fake_close_driver)
    monkeypatch.setattr(worker, "init_driver", fake_init_driver)

    worker.rebuild_driver()

    assert events == [("quit", "rebuild"), ("create", "new_driver")]
    assert worker.driver is new_driver


def test_stop_runtime_terminates_tracked_app_owned_orphan_chrome(tmp_path, monkeypatch):
    logs = []
    storage = SimpleNamespace(insert_runtime_log=lambda **kwargs: logs.append(kwargs))
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue(), storage=storage)
    driver = _FakeWarmDriver()
    worker.driver = driver
    worker.state.chromedriver_pid = 321
    worker.state.tracked_chrome_pids = [654, 655]
    worker.state.orphan_app_chrome_pids = [655]
    terminated = []

    monkeypatch.setattr(worker, "_verify_chromedriver_stopped", lambda pid: True)
    monkeypatch.setattr(worker, "_refresh_process_tracking", lambda **kwargs: {
        "tracked_chrome_pids": worker.state.tracked_chrome_pids,
        "orphan_app_chrome_pids": worker.state.orphan_app_chrome_pids,
    })
    monkeypatch.setattr(worker, "_process_exists", lambda pid: pid in {654, 655})
    monkeypatch.setattr(
        worker,
        "_terminate_app_owned_chrome_pids",
        lambda *, reason, pids=None: terminated.append((reason, list(pids or []))) or [
            {"pid": pid, "terminated": True} for pid in (pids or [])
        ],
    )

    worker.close_driver(reason="shutdown")

    assert driver.quit_calls == 1
    assert terminated == [("after_driver_quit:shutdown", [654, 655])]
    assert any(log["message"] == "runtime_stop_orphan_chrome_detected" for log in logs)
    assert any(log["message"] == "selenium_driver_quit_finished" for log in logs)


def test_profile_lock_cleanup_closes_all_marker_chrome_pids_before_new_driver(tmp_path, monkeypatch):
    worker = SeleniumWorker(
        cfg=_warm_cfg(
            tmp_path,
            chrome_user_data_dir=str(tmp_path / "chrome-profile"),
            chrome_profile_dir="Default",
        ),
        job_queue=queue.Queue(),
    )
    worker.state.driver_session_id = "old-session"
    worker.state.chromedriver_pid = 321
    worker.state.chrome_pid = 654
    worker.state.tracked_chrome_pids = [654, 655, 656]
    worker._write_app_driver_marker()

    marked_terminations = []
    chrome_terminations = []

    monkeypatch.setattr(
        worker,
        "_terminate_marked_process",
        lambda *, pid, name, expected_command_parts: marked_terminations.append(
            (pid, name, tuple(expected_command_parts))
        )
        or {"pid": pid, "terminated": True},
    )
    monkeypatch.setattr(
        worker,
        "_terminate_app_owned_chrome_pids",
        lambda *, reason, pids=None: chrome_terminations.append((reason, list(pids or [])))
        or [{"pid": pid, "terminated": True} for pid in (pids or [])],
    )
    monkeypatch.setattr(worker, "_process_exists", lambda pid: False)

    closed = worker._close_old_app_driver_processes(reason="profile_lock_detected")

    assert closed is True
    assert marked_terminations == [(321, "chromedriver", ("chromedriver",))]
    assert chrome_terminations == [("old_app_driver:profile_lock_detected", [654, 655, 656])]


def test_old_app_chromedriver_is_closed_by_debug_log_dir_when_marker_missing(tmp_path, monkeypatch):
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    command_line = f"C:\\tools\\chromedriver.exe --log-path={worker._app_debug_log_dir}\\chromedriver_1.log"
    terminations = []

    monkeypatch.setattr(worker, "_read_app_driver_marker", lambda: {})
    monkeypatch.setattr(
        worker,
        "_list_processes",
        lambda: [
            {"pid": 321, "name": "chromedriver.exe", "command_line": command_line, "parent_pid": 0},
            {"pid": 999, "name": "chromedriver.exe", "command_line": "C:\\other\\chromedriver.exe", "parent_pid": 0},
        ],
    )
    monkeypatch.setattr(worker, "_refresh_process_tracking", lambda **kwargs: {})
    monkeypatch.setattr(
        worker,
        "_terminate_marked_process",
        lambda *, pid, name, expected_command_parts: terminations.append((pid, name, tuple(expected_command_parts)))
        or {"pid": pid, "terminated": True},
    )

    assert worker._close_old_app_driver_processes(reason="startup") is True
    assert terminations == [(321, "chromedriver", (worker._app_debug_log_dir,))]


def test_unrelated_chrome_process_is_not_killed(tmp_path, monkeypatch):
    worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
    terminations = []

    monkeypatch.setattr(worker, "_refresh_process_tracking", lambda **kwargs: {})
    monkeypatch.setattr(
        worker,
        "_process_command_line",
        lambda pid: "chrome.exe --user-data-dir=C:\\Users\\SyntheticUser\\Chrome",
    )
    monkeypatch.setattr(
        worker,
        "_terminate_marked_process",
        lambda *, pid, name, expected_command_parts: terminations.append((pid, name, expected_command_parts))
        or {"pid": pid, "terminated": True},
    )

    assert worker._terminate_app_owned_chrome_pids(reason="test", pids=[777]) == [
        {"pid": 777, "terminated": False, "reason": "not_app_owned"}
    ]
    assert terminations == []


def test_runtime_prewarm_helper_uses_legacy_config_and_opens_warm_tabs(tmp_path):
    class _ConfigManager:
        def get_timer_settings(self):
            return {"enabled": False, "interval_seconds": 60}

    class _Storage:
        def __init__(self):
            self.logs = []

        def insert_runtime_log(self, **kwargs):
            self.logs.append(kwargs)

        def list_watchlist(self, enabled=True, limit=2000):
            return [_warm_item("1895844")]

    class _Dispatcher:
        worker = SimpleNamespace(state=SimpleNamespace(ready=True))

        def __init__(self):
            self.config = None
            self.prewarm_calls = 0
            self.configure_calls = []
            self.preload_calls = 0

        def set_runtime_config(self, config, *, prewarm_skip_reason=None):
            self.config = dict(config)

        def prewarm_browser(self, **kwargs):
            self.prewarm_calls += 1
            return {"ok": True, "skipped": False, "reason": ""}

        def configure_warm_tabs(self, items):
            self.configure_calls.append(list(items))
            return {"ok": True, "configured": len(items)}

        def preload_warm_tabs_now(self):
            self.preload_calls += 1
            return {"ok": True, "opened": 1}

    manager = RuntimeManager(
        paths=SimpleNamespace(repo_root=tmp_path, app_root=tmp_path),
        config_manager=_ConfigManager(),
    )
    storage = _Storage()
    dispatcher = _Dispatcher()
    cfg = _warm_cfg(tmp_path)
    try:
        worker = asyncio.run(
            manager._prewarm_selenium_for_runtime(
                cfg=cfg,
                storage=storage,
                selenium_dispatcher=dispatcher,
            )
        )
    finally:
        manager.close()

    messages = [log["message"] for log in storage.logs]
    assert worker is dispatcher.worker
    assert dispatcher.prewarm_calls == 1
    assert dispatcher.configure_calls and dispatcher.configure_calls[0][0]["article_number"] == "1895844"
    assert dispatcher.preload_calls == 1
    assert "selenium_config_normalized" in messages
    assert "selenium_prewarm_requested" in messages
    assert "watchlist_warm_tabs_preload_finished" in messages


def test_runtime_prewarm_with_warm_tabs_creates_one_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(MediaMarktWorkerCase, "_wait_dom_settle", staticmethod(lambda driver, timeout=5.0: None))
    monkeypatch.setattr(
        MediaMarktWorkerCase,
        "detect_pdp_dom_status",
        staticmethod(lambda driver: driver.dom_status),
    )

    class _ConfigManager:
        def get_timer_settings(self):
            return {"enabled": False, "interval_seconds": 60}

    class _Storage:
        def __init__(self):
            self.logs = []

        def insert_runtime_log(self, **kwargs):
            self.logs.append(kwargs)

        def list_watchlist(self, enabled=True, limit=2000):
            return [_warm_item("1895844"), _warm_item("1900000")]

    driver = _FakeWarmDriver()
    created = []

    def worker_factory():
        worker = SeleniumWorker(cfg=_warm_cfg(tmp_path), job_queue=queue.Queue())
        worker.init_driver = lambda: created.append(driver) or driver
        return worker

    manager = RuntimeManager(
        paths=SimpleNamespace(repo_root=tmp_path, app_root=tmp_path),
        config_manager=_ConfigManager(),
    )
    dispatcher = SeleniumDispatcher(queue.Queue(), worker_factory=worker_factory)
    storage = _Storage()
    try:
        worker = asyncio.run(
            manager._prewarm_selenium_for_runtime(
                cfg=_warm_cfg(tmp_path),
                storage=storage,
                selenium_dispatcher=dispatcher,
            )
        )
        snapshot = dispatcher.lifecycle_snapshot()
    finally:
        dispatcher.stop(timeout=1)
        manager.close()

    assert worker is dispatcher.worker
    assert created == [driver]
    assert driver.window_handles == ["main", "tab-1"]
    assert driver.get_calls[0][0] == "main"
    assert snapshot["driver_exists"] is True
    assert snapshot["warm_tabs_count"] == 2


@pytest.mark.parametrize(
    "env_text",
    [
        "ACTION_MODE=selenium\nSELENIUM_PREWARM=true\n",
        "\n".join(
            [
                "ACTION_MODE=selenium",
                "SELENIUM_PREWARM=false",
                "SELENIUM_PREWARM_ENABLED=true",
                "SELENIUM_PREWARM_ON_RUNTIME_START=true",
                "SELENIUM_KEEP_BROWSER_ALIVE=true",
            ]
        ),
    ],
)
def test_runtime_prewarm_env_configs_create_driver_on_start(tmp_path, monkeypatch, env_text):
    for key in (
        "ACTION_MODE",
        "SELENIUM_PREWARM",
        "SELENIUM_PREWARM_ENABLED",
        "SELENIUM_PREWARM_ON_RUNTIME_START",
        "SELENIUM_KEEP_BROWSER_ALIVE",
    ):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    cfg = load_config(base_dir=tmp_path)
    driver = _FakeWarmDriver()
    created = []

    class _ConfigManager:
        def get_timer_settings(self):
            return {"enabled": False, "interval_seconds": 60}

    class _Storage:
        def insert_runtime_log(self, **kwargs):
            pass

        def list_watchlist(self, enabled=True, limit=2000):
            return []

    def worker_factory():
        worker = SeleniumWorker(cfg=cfg, job_queue=queue.Queue())
        worker.init_driver = lambda: created.append(driver) or driver
        return worker

    manager = RuntimeManager(
        paths=SimpleNamespace(repo_root=tmp_path, app_root=tmp_path),
        config_manager=_ConfigManager(),
    )
    dispatcher = SeleniumDispatcher(queue.Queue(), worker_factory=worker_factory)
    try:
        worker = asyncio.run(
            manager._prewarm_selenium_for_runtime(
                cfg=cfg,
                storage=_Storage(),
                selenium_dispatcher=dispatcher,
            )
        )
        snapshot = dispatcher.lifecycle_snapshot()
    finally:
        dispatcher.stop(timeout=1)
        manager.close()

    assert worker is dispatcher.worker
    assert created == [driver]
    assert snapshot["driver_exists"] is True
    assert snapshot["prewarmed"] is True
    assert snapshot["config"]["prewarm_enabled"] is True
    assert driver.quit_calls == 1


def test_runtime_prewarm_helper_skips_when_disabled(tmp_path):
    class _ConfigManager:
        def get_timer_settings(self):
            return {"enabled": False, "interval_seconds": 60}

    class _Storage:
        def __init__(self):
            self.logs = []

        def insert_runtime_log(self, **kwargs):
            self.logs.append(kwargs)

    class _Dispatcher:
        def __init__(self):
            self.prewarm_calls = 0

        def set_runtime_config(self, config, *, prewarm_skip_reason=None):
            self.config = dict(config)
            self.skip = prewarm_skip_reason

        def prewarm_browser(self, **kwargs):
            self.prewarm_calls += 1
            return {"ok": True}

    manager = RuntimeManager(
        paths=SimpleNamespace(repo_root=tmp_path, app_root=tmp_path),
        config_manager=_ConfigManager(),
    )
    storage = _Storage()
    dispatcher = _Dispatcher()
    cfg = _warm_cfg(tmp_path, selenium_prewarm_enabled=False)
    try:
        worker = asyncio.run(
            manager._prewarm_selenium_for_runtime(
                cfg=cfg,
                storage=storage,
                selenium_dispatcher=dispatcher,
            )
        )
    finally:
        manager.close()

    assert worker is None
    assert dispatcher.prewarm_calls == 0
    assert dispatcher.skip == "prewarm_disabled"
    assert any(
        log["message"] == "selenium_prewarm_skipped"
        and log["details"]["reason"] == "prewarm_disabled"
        for log in storage.logs
    )


def test_runtime_start_twice_schedules_only_one_runtime(tmp_path):
    class _ConfigManager:
        def get_timer_settings(self):
            return {"enabled": False, "interval_seconds": 60}

    manager = RuntimeManager(
        paths=SimpleNamespace(repo_root=tmp_path, app_root=tmp_path),
        config_manager=_ConfigManager(),
    )
    starts = []
    finish = False

    async def fake_runtime_main(*, kind, trigger):
        nonlocal finish
        starts.append((kind, trigger))
        while not finish:
            await asyncio.sleep(0.01)

    manager._runtime_main = fake_runtime_main
    try:
        first = manager.start()
        deadline = time.time() + 1
        while not starts and time.time() < deadline:
            time.sleep(0.01)
        second = manager.start()

        assert first["running"] is True
        assert second["running"] is True
        assert second["message"] == "Runtime already running."
        assert starts == [("continuous", "manual")]
    finally:
        finish = True
        deadline = time.time() + 1
        while manager.is_running() and time.time() < deadline:
            time.sleep(0.01)
        manager.close()


def test_runtime_restart_stops_before_starting(tmp_path):
    class _ConfigManager:
        def get_timer_settings(self):
            return {"enabled": False, "interval_seconds": 60}

    manager = RuntimeManager(
        paths=SimpleNamespace(repo_root=tmp_path, app_root=tmp_path),
        config_manager=_ConfigManager(),
    )
    events = []
    manager.stop = lambda: events.append("stop") or {"running": False}
    manager.start = lambda: events.append("start") or {"running": True}
    try:
        manager.restart()
        assert events == ["stop", "start"]
    finally:
        manager.close()


def test_manual_watchlist_scan_reuses_runtime_dispatcher(tmp_path, monkeypatch):
    dispatcher = SeleniumDispatcher(queue.Queue())
    driver = _FakeWarmDriver()
    dispatcher._worker = SimpleNamespace(driver=driver)
    captured = []

    class _Config:
        telegram_bot_token = ""
        telegram_chat_id = ""
        proxy_enabled = False
        proxy_host = ""
        proxy_port = 0
        proxy_type = "http"

        def resolved_db_path(self):
            return tmp_path / "watchlist.sqlite3"

        def watchlist_request_timeout_seconds(self):
            return 1

    class _ConfigManager:
        def load_app_config(self):
            return _Config()

    class _RuntimeManager:
        def current_selenium_dispatcher(self):
            return dispatcher

    async def fake_scan_once(self, session, *, site=None, product_key=None, item_id=None):
        captured.append((self.selenium_dispatcher, self.selenium_dispatcher.worker.driver))
        return {"ok": True, "checked": 0}

    monkeypatch.setattr("pokemon_parser.engine.watchlist.WatchlistTracker.scan_once", fake_scan_once)

    manager = WatchlistManager(config_manager=_ConfigManager(), runtime_manager=_RuntimeManager())
    result = manager.scan_now(site="mediamarkt")

    assert result["ok"] is True
    assert captured == [(dispatcher, driver)]


def test_manual_watchlist_scan_without_runtime_does_not_spawn_dispatcher(tmp_path, monkeypatch):
    captured = []

    class _Config:
        telegram_bot_token = ""
        telegram_chat_id = ""
        proxy_enabled = False
        proxy_host = ""
        proxy_port = 0
        proxy_type = "http"

        def resolved_db_path(self):
            return tmp_path / "watchlist.sqlite3"

        def watchlist_request_timeout_seconds(self):
            return 1

    class _ConfigManager:
        def load_app_config(self):
            return _Config()

    async def fake_scan_once(self, session, *, site=None, product_key=None, item_id=None):
        captured.append(self.selenium_dispatcher)
        return {"ok": True, "checked": 0}

    monkeypatch.setattr("pokemon_parser.engine.watchlist.WatchlistTracker.scan_once", fake_scan_once)

    manager = WatchlistManager(config_manager=_ConfigManager(), runtime_manager=None)
    result = manager.scan_now(site="mediamarkt")

    assert result["ok"] is True
    assert captured == [None]


def test_chrome_profile_bootstrap_is_blocked_while_runtime_running(monkeypatch):
    class _RuntimeManager:
        def is_running(self):
            return True

    monkeypatch.setattr(settings_routes, "get_runtime_manager", lambda: _RuntimeManager())
    monkeypatch.setattr(
        settings_routes,
        "get_config_manager",
        lambda: SimpleNamespace(create_chrome_profile=lambda: pytest.fail("must not launch Chrome")),
    )

    with pytest.raises(HTTPException) as excinfo:
        settings_routes.create_chrome_profile()

    assert excinfo.value.status_code == 409


def test_dashboard_start_runtime_does_not_trigger_profile_bootstrap_endpoint():
    source = Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx"
    app_source = source.read_text(encoding="utf-8")
    run_handler = app_source[
        app_source.index('if (action === "run")') : app_source.index('} else if (action === "stop")')
    ]

    assert "api.runRuntime()" in run_handler
    assert "createChromeProfile" not in run_handler
    assert "launchChromeProfile" not in run_handler
    assert "/api/chrome-profile/create" not in run_handler


def test_runtime_overview_selenium_lifecycle_fields_are_present():
    snapshot = SeleniumDispatcher(queue.Queue()).lifecycle_snapshot()

    for field in (
        "dispatcher_exists",
        "dispatcher_running",
        "worker_thread_alive",
        "driver_exists",
        "driver_session_id",
        "browser_started_at",
        "last_driver_create_at",
        "last_driver_quit_at",
        "chromedriver_pid",
        "chrome_pid",
        "tracked_chrome_pids",
        "orphan_app_chrome_pids",
        "selenium_window_count",
        "selenium_top_level_window_ids",
        "selenium_top_level_window_count",
        "selenium_top_level_window_id_by_handle",
        "window_handles_count",
        "window_handles_current_urls",
        "last_window_snapshot_at",
        "last_start_at",
        "last_stop_at",
        "last_error",
        "lifecycle_state",
        "config",
        "prewarmed",
        "last_prewarm_skip_reason",
        "last_prewarm_error",
        "warm_tabs_enabled",
        "warm_tabs_count",
        "warm_tabs_max",
        "warm_tabs",
        "warm_tab_urls",
        "active_action",
        "active_worker_action",
        "worker_busy",
        "warm_refresh_running",
        "warm_refresh_paused_reason",
        "challenge_detected_count",
        "challenge_sources",
        "challenge_manual_action_required",
        "last_action_latency_seconds",
        "last_button_search_latency_seconds",
        "duplicate_start_ignored_count",
        "duplicate_start_guard_count",
    ):
        assert field in snapshot


def test_runtime_overview_exposes_normalized_selenium_config(tmp_path):
    snapshot = RuntimeManager._build_selenium_snapshot(None, _warm_cfg(tmp_path))

    assert snapshot["config"]["action_mode"] == "selenium"
    assert snapshot["config"]["prewarm_enabled"] is True
    assert snapshot["config"]["prewarm_on_runtime_start"] is True
    assert snapshot["config"]["keep_browser_alive"] is True
    assert snapshot["config"]["warm_tabs_enabled"] is True
