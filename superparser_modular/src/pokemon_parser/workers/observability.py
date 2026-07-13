from __future__ import annotations

import json
import os
import re
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from pokemon_parser.models import SeleniumJob
from pokemon_parser.utils.logging_setup import resolve_debug_log_dir
from pokemon_parser.utils.selenium_diagnostics import safe_filename_part, timestamp_for_filename

MAX_TEXT_LENGTH = 300
MAX_BUTTONS = 30
MAX_TRACEBACK_LENGTH = 1200
SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "authorization",
    "cookie",
    "card_number",
    "cardnumber",
    "cvv",
    "cvc",
    "expiry",
)


def low_level_debug_enabled(cfg: Any) -> bool:
    value = getattr(cfg, "worker_low_level_debug", None)
    if value is None:
        value = os.environ.get("WORKER_LOW_LEVEL_DEBUG", "")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def truncate_text(value: Any, limit: int = MAX_TEXT_LENGTH) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    text = value.replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted-url>"
    if not parsed.scheme or not parsed.hostname:
        return truncate_text(value)
    hostname = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return "<redacted-url>"
    if port:
        hostname = f"{hostname}:{port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def compact_payload(value: Any, *, _key: str = "") -> Any:
    normalized_key = _key.replace("-", "_").lower()
    if normalized_key and any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): compact_payload(v, _key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        limit = MAX_BUTTONS if value and isinstance(value[0], dict) else 50
        items = [compact_payload(item, _key=_key) for item in list(value)[:limit]]
        if len(value) > limit:
            items.append(f"...<truncated {len(value) - limit} items>")
        return items
    if isinstance(value, str):
        if normalized_key.endswith("url") or normalized_key in {"href", "url_before_click", "url_after_click"}:
            return _safe_url(value)
        value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", value)
        value = re.sub(r"\b\d{13,19}\b", "<redacted-card-number>", value)
        value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<redacted-email>", value)
        return truncate_text(value)
    return value


def safe_current_url(driver: Any) -> str:
    try:
        return _safe_url(str(driver.current_url or ""))
    except Exception:
        return ""


def safe_title(driver: Any) -> str:
    try:
        return str(driver.title or "")
    except Exception:
        return ""


def safe_window_handle(driver: Any) -> str:
    try:
        return str(driver.current_window_handle or "")
    except Exception:
        return ""


def safe_ready_state(driver: Any) -> str:
    try:
        return str(driver.execute_script("return document.readyState") or "")
    except Exception:
        return ""


def summarize_element(element: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    try:
        summary["tag"] = str(getattr(element, "tag_name", "") or "")
    except Exception:
        summary["tag"] = ""
    try:
        summary["text"] = truncate_text(getattr(element, "text", "") or "")
    except Exception:
        summary["text"] = ""
    for attr_name, output_name in (
        ("id", "id"),
        ("data-test", "data_test"),
        ("aria-label", "aria_label"),
        ("class", "class"),
        ("href", "href"),
        ("disabled", "disabled_attr"),
        ("aria-disabled", "aria_disabled"),
    ):
        try:
            summary[output_name] = truncate_text(element.get_attribute(attr_name) or "")
        except Exception:
            summary[output_name] = ""
    try:
        summary["visible"] = bool(element.is_displayed())
    except Exception:
        summary["visible"] = None
    try:
        summary["enabled"] = bool(element.is_enabled())
    except Exception:
        summary["enabled"] = None
    try:
        summary["rect"] = dict(element.rect or {})
    except Exception:
        summary["rect"] = {}
    return compact_payload(summary)


def summarize_visible_buttons(driver: Any, limit: int = MAX_BUTTONS) -> list[dict[str, Any]]:
    try:
        payload = driver.execute_script(
            """
            const limit = arguments[0] || 30;
            return Array.from(document.querySelectorAll('button,a[role="button"],a[href]'))
              .map((el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const visible = rect.width > 0 && rect.height > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none';
                return {
                  tag: el.tagName.toLowerCase(),
                  text: (el.innerText || el.textContent || '').trim(),
                  id: el.id || '',
                  data_test: el.getAttribute('data-test') || '',
                  aria_label: el.getAttribute('aria-label') || '',
                  class: el.getAttribute('class') || '',
                  href: el.getAttribute('href') || '',
                  visible,
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
              })
              .filter((item) => item.visible)
              .slice(0, limit);
            """,
            limit,
        )
        return compact_payload(list(payload or []))
    except Exception:
        return []


def summarize_alerts_toasts(driver: Any) -> list[dict[str, Any]]:
    try:
        payload = driver.execute_script(
            """
            const selectors = [
              '[role="alert"]',
              '[aria-live]',
              '[class*="toast" i]',
              '[class*="snackbar" i]',
              '[class*="alert" i]',
              '[data-test*="toast" i]',
              '[data-test*="snackbar" i]',
              '[data-test*="alert" i]'
            ];
            const seen = new Set();
            const rows = [];
            for (const selector of selectors) {
              for (const el of Array.from(document.querySelectorAll(selector))) {
                if (seen.has(el)) continue;
                seen.add(el);
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const visible = rect.width > 0 && rect.height > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none';
                const text = (el.innerText || el.textContent || '').trim();
                if (!visible || !text) continue;
                rows.push({
                  selector,
                  text,
                  role: el.getAttribute('role') || '',
                  aria_live: el.getAttribute('aria-live') || '',
                  data_test: el.getAttribute('data-test') || '',
                  class: el.getAttribute('class') || ''
                });
              }
            }
            return rows.slice(0, 20);
            """
        )
        return compact_payload(list(payload or []))
    except Exception:
        return []


MEDIAMARKT_UNAVAILABLE_PHRASES = (
    "een of meer producten zijn niet langer verkrijgbaar",
    "jouw winkelwagen is gewijzigd",
    "helaas zijn deze producten niet verkrijgbaar",
    "niet langer verkrijgbaar",
    "niet verkrijgbaar",
    "helaas geen bezorging mogelijk",
    "dit product is binnenkort weer beschikbaar",
)


def detect_mediamarkt_unavailable_markers(driver: Any) -> dict[str, Any]:
    try:
        payload = driver.execute_script(
            """
            const phrases = arguments[0];
            const text = ((document.body && document.body.innerText) || '').toLowerCase();
            const markers = [];
            for (const phrase of phrases) {
              if (text.includes(phrase)) markers.push(phrase);
            }
            const disabledCheckoutButtons = [];
            for (const el of Array.from(document.querySelectorAll('button,a[role="button"]'))) {
              const label = [
                el.innerText || el.textContent || '',
                el.getAttribute('aria-label') || '',
                el.getAttribute('data-test') || ''
              ].join(' ').trim();
              const lowered = label.toLowerCase();
              if (!lowered.includes('ik ga bestellen')) continue;
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              const visible = rect.width > 0 && rect.height > 0 &&
                style.visibility !== 'hidden' && style.display !== 'none';
              const disabled = Boolean(el.disabled) ||
                el.getAttribute('disabled') !== null ||
                el.getAttribute('aria-disabled') === 'true';
              if (visible && disabled) {
                disabledCheckoutButtons.push({
                  tag: el.tagName.toLowerCase(),
                  text: (el.innerText || el.textContent || '').trim(),
                  id: el.id || '',
                  data_test: el.getAttribute('data-test') || '',
                  aria_label: el.getAttribute('aria-label') || '',
                  disabled_attr: el.getAttribute('disabled') || '',
                  aria_disabled: el.getAttribute('aria-disabled') || '',
                  rect: {
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height)
                  }
                });
              }
            }
            return {markers, disabled_checkout_buttons: disabledCheckoutButtons.slice(0, 10)};
            """,
            list(MEDIAMARKT_UNAVAILABLE_PHRASES),
        ) or {}
    except Exception:
        payload = {
            "markers": [],
            "disabled_checkout_buttons": [],
        }
    markers = list(dict.fromkeys(payload.get("markers") or []))
    disabled_buttons = list(payload.get("disabled_checkout_buttons") or [])
    return compact_payload(
        {
            "found": bool(markers or disabled_buttons),
            "markers": markers,
            "disabled_checkout_buttons": disabled_buttons,
        }
    )


def detect_queue_markers(driver: Any, site: str | None = None) -> dict[str, Any]:
    try:
        from pokemon_parser.workers.queue import detect_queue_page

        state = detect_queue_page(driver, site)
        return {
            "in_queue": state.in_queue,
            "url": truncate_text(state.url),
            "title": truncate_text(state.title),
            "signals": list(state.signals),
        }
    except Exception as exc:
        return {"in_queue": False, "error": f"{type(exc).__name__}: {truncate_text(str(exc))}"}


def driver_context(driver: Any) -> dict[str, Any]:
    return {
        "current_url": safe_current_url(driver),
        "page_title": safe_title(driver),
        "window_handle": safe_window_handle(driver),
        "document_ready_state": safe_ready_state(driver),
        "active_tab_url": safe_current_url(driver),
    }


class WorkerActionTimeline:
    def __init__(self, *, cfg: Any, job: SeleniumJob, action_id: str | None = None) -> None:
        self.cfg = cfg
        self.job = job
        self.action_id = action_id or job.action_id
        self.started_monotonic = time.monotonic()
        self.events: list[dict[str, Any]] = []
        self.timings: dict[str, float] = {}
        self.current_step = ""
        self.last_successful_step = ""
        self.timed_out_selector = ""
        self.timeout_seconds: float | None = None
        self.timeline_path: str = ""
        self.total_job_received_to_click_ms: float | None = None
        self.total_job_received_to_checkout_ms: float | None = None

    def _base(self) -> dict[str, Any]:
        return {
            "timestamp": _utc_now_iso(),
            "action_id": self.action_id,
            "site": self.job.site,
            "external_id": self.job.target.external_id,
            "case": self.job.case,
        }

    def record(self, event_name: str, **context: Any) -> dict[str, Any]:
        event = self._base()
        event["event"] = event_name
        if "step_name" not in context:
            context["step_name"] = event_name
        if context.get("step_name"):
            self.current_step = str(context["step_name"])
        if context.get("result") == "success" and context.get("step_name"):
            self.last_successful_step = str(context["step_name"])
        if context.get("duration_ms") is not None:
            try:
                duration_ms = round(float(context["duration_ms"]), 3)
                context["duration_ms"] = duration_ms
                self.timings[str(context["step_name"])] = duration_ms
            except Exception:
                pass
        if context.get("selector"):
            self.timed_out_selector = str(context["selector"])
        if context.get("condition"):
            self.timed_out_selector = self.timed_out_selector or str(context["condition"])
        if context.get("timeout_seconds") is not None:
            try:
                self.timeout_seconds = float(context["timeout_seconds"])
            except Exception:
                pass
        if context.get("total_job_received_to_click_ms") is not None:
            self.total_job_received_to_click_ms = float(context["total_job_received_to_click_ms"])
        if context.get("total_job_received_to_checkout_ms") is not None:
            self.total_job_received_to_checkout_ms = float(context["total_job_received_to_checkout_ms"])
        event.update(compact_payload(context))
        self.events.append(event)
        return event

    @contextmanager
    def timed_step(self, name: str, **context: Any) -> Iterator[dict[str, Any]]:
        started = time.monotonic()
        self.record(f"{name}_started", step_name=name, **context)
        try:
            yield context
        except Exception as exc:
            self.record(
                f"{name}_finished",
                step_name=name,
                result="exception",
                duration_ms=(time.monotonic() - started) * 1000,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        else:
            self.record(
                f"{name}_finished",
                step_name=name,
                result="success",
                duration_ms=(time.monotonic() - started) * 1000,
            )

    def write(self, *, result: str, reason: str = "") -> str:
        if self.timeline_path:
            return self.timeline_path
        base_dir = resolve_debug_log_dir(getattr(self.cfg, "base_dir", None)) / "worker_timelines"
        base_dir.mkdir(parents=True, exist_ok=True)
        prefix = "_".join(
            [
                timestamp_for_filename(),
                safe_filename_part(self.job.site),
                safe_filename_part(self.job.target.external_id),
                safe_filename_part(self.action_id),
            ]
        )
        path = base_dir / f"{prefix}.jsonl"
        self.record(
            "worker_timeline_written",
            step_name="timeline_write",
            result=result,
            reason=reason,
            event_count=len(self.events),
        )
        with path.open("w", encoding="utf-8") as fh:
            for event in self.events:
                fh.write(json.dumps(compact_payload(event), ensure_ascii=False, separators=(",", ":")) + "\n")
        self.timeline_path = str(path)
        return self.timeline_path


def timed_wait(
    *,
    timeline: WorkerActionTimeline,
    driver: Any,
    wait_name: str,
    condition: Callable[[Any], Any],
    timeout: float,
    selector_info: str = "",
    poll_frequency: float = 0.1,
) -> Any:
    started = time.monotonic()
    timeline.record(
        "wait_started",
        wait_name=wait_name,
        step_name=wait_name,
        selector=selector_info,
        condition=getattr(condition, "__name__", repr(condition)),
        timeout_seconds=timeout,
        poll_frequency=poll_frequency,
        **driver_context(driver),
    )
    try:
        result = WebDriverWait(driver, timeout, poll_frequency=poll_frequency).until(condition)
        duration_ms = (time.monotonic() - started) * 1000
        matched_count = None
        first_summary = None
        if selector_info:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector_info)
                matched_count = len(elements)
                if elements:
                    first_summary = summarize_element(elements[0])
            except Exception:
                pass
        timeline.record(
            "wait_finished",
            wait_name=wait_name,
            step_name=wait_name,
            selector=selector_info,
            timeout_seconds=timeout,
            poll_frequency=poll_frequency,
            result="success",
            duration_ms=duration_ms,
            matched_element_count=matched_count,
            first_matched_element=first_summary,
            **driver_context(driver),
        )
        return result
    except TimeoutException:
        duration_ms = (time.monotonic() - started) * 1000
        timeline.record(
            "wait_finished",
            wait_name=wait_name,
            step_name=wait_name,
            selector=selector_info,
            timeout_seconds=timeout,
            poll_frequency=poll_frequency,
            result="timeout",
            duration_ms=duration_ms,
            **driver_context(driver),
        )
        raise
    except Exception as exc:
        duration_ms = (time.monotonic() - started) * 1000
        timeline.record(
            "wait_finished",
            wait_name=wait_name,
            step_name=wait_name,
            selector=selector_info,
            timeout_seconds=timeout,
            poll_frequency=poll_frequency,
            result="exception",
            duration_ms=duration_ms,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=truncate_text(traceback.format_exc(), MAX_TRACEBACK_LENGTH),
            **driver_context(driver),
        )
        raise


def timed_click(
    *,
    timeline: WorkerActionTimeline,
    driver: Any,
    click_name: str,
    element: Any,
    method: str = "native",
    selector_strategy: str = "",
    short_delay_seconds: float = 0.05,
) -> None:
    started = time.monotonic()
    before_url = safe_current_url(driver)
    element_summary = summarize_element(element)
    timeline.record(
        "click_started",
        click_name=click_name,
        step_name=click_name,
        selector_strategy=selector_strategy,
        click_method=method,
        element=element_summary,
        url_before_click=before_url,
        **driver_context(driver),
    )
    try:
        if method == "js":
            driver.execute_script("arguments[0].click();", element)
        else:
            element.click()
        if short_delay_seconds > 0:
            time.sleep(short_delay_seconds)
        timeline.record(
            "click_finished",
            click_name=click_name,
            step_name=click_name,
            selector_strategy=selector_strategy,
            click_method=method,
            element=element_summary,
            result="success",
            duration_ms=(time.monotonic() - started) * 1000,
            url_before_click=before_url,
            url_after_click=safe_current_url(driver),
            **driver_context(driver),
        )
    except Exception as exc:
        timeline.record(
            "click_finished",
            click_name=click_name,
            step_name=click_name,
            selector_strategy=selector_strategy,
            click_method=method,
            element=element_summary,
            result="exception",
            duration_ms=(time.monotonic() - started) * 1000,
            url_before_click=before_url,
            url_after_click=safe_current_url(driver),
            error_type=type(exc).__name__,
            error=str(exc),
            **driver_context(driver),
        )
        raise
