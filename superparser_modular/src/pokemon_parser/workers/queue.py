from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from selenium.webdriver.common.by import By

from pokemon_parser.config import AppConfig

if TYPE_CHECKING:
    from pokemon_parser.workers.trace import WorkerTraceLogger

logger = logging.getLogger(__name__)


class QueueTimeoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class QueueState:
    in_queue: bool
    url: str
    title: str
    signals: tuple[str, ...]


URL_KEYWORDS = (
    "queue",
    "waiting",
    "wachtrij",
    "wait",
    "traffic",
    "waitingroom",
    "waiting-room",
)

TEXT_PHRASES = (
    "queue",
    "waiting room",
    "you are in line",
    "you are in a queue",
    "estimated wait",
    "please wait",
    "waiting",
    "wachtrij",
    "even geduld",
    "in de rij",
    "u staat in de rij",
    "je staat in de rij",
    "virtuele wachtruimte",
    "virtual waiting room",
    "heavy traffic",
    "traffic is high",
    "we are experiencing high demand",
)

DOM_MARKERS = (
    "[id*='queue-it']",
    "[class*='queue-it']",
    "[data-queueit]",
    "[data-queue-it]",
    "iframe[src*='queue-it']",
    "iframe[src*='queue']",
    "[id*='waiting-room']",
    "[class*='waiting-room']",
    "[aria-label*='queue' i]",
    "[aria-label*='wachtrij' i]",
)


def _safe_current_url(driver) -> str:
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def _safe_title(driver) -> str:
    try:
        return driver.title or ""
    except Exception:
        return ""


def _page_text(driver) -> str:
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


def _visible_dom_markers(driver) -> list[str]:
    matches: list[str] = []
    for selector in DOM_MARKERS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue

        for element in elements[:3]:
            try:
                if element.is_displayed():
                    matches.append(f"dom:{selector}")
                    break
            except Exception:
                matches.append(f"dom:{selector}")
                break
    return matches


def detect_queue_page(driver, site: str | None = None) -> QueueState:
    url = _safe_current_url(driver)
    title = _safe_title(driver)
    text = _page_text(driver)
    lowered_url = url.lower()
    lowered_title = title.lower()

    signals: list[str] = []
    for keyword in URL_KEYWORDS:
        if keyword in lowered_url:
            signals.append(f"url:{keyword}")

    for phrase in TEXT_PHRASES:
        if phrase in text or phrase in lowered_title:
            signals.append(f"text:{phrase}")

    signals.extend(_visible_dom_markers(driver))

    if site == "pocketgames" and "shopify" in text and "queue" in text:
        signals.append("site:pocketgames_shopify_queue")

    return QueueState(
        in_queue=bool(signals),
        url=url,
        title=title,
        signals=tuple(dict.fromkeys(signals)),
    )


def wait_if_queue(
    driver,
    *,
    site: str | None,
    phase: str,
    cfg: AppConfig,
    trace: "WorkerTraceLogger | None" = None,
    timeout: float | None = None,
    poll_seconds: float | None = None,
) -> QueueState:
    if not cfg.queue_check_enabled:
        if trace is not None:
            trace.step(
                "Queue check skipped",
                {"phase": phase, "reason": "disabled"},
            )
        return QueueState(False, _safe_current_url(driver), _safe_title(driver), ())

    timeout = cfg.queue_wait_timeout_seconds if timeout is None else max(1.0, float(timeout))
    poll_seconds = cfg.queue_poll_seconds if poll_seconds is None else max(0.1, float(poll_seconds))
    queue_update_seconds = max(5.0, float(cfg.worker_trace_queue_update_seconds))

    if trace is not None:
        trace.step("Checking queue page", {"phase": phase, "url": _safe_current_url(driver)})

    state = detect_queue_page(driver, site)
    if not state.in_queue:
        if trace is not None:
            trace.step("Queue check passed", {"phase": phase, "url": state.url})
        return state

    logger.warning("[queue][%s] detected phase=%s url=%s signals=%s", site, phase, state.url, state.signals)
    if trace is not None:
        trace.warning(
            "Queue detected",
            {
                "phase": phase,
                "url": state.url,
                "signals": list(state.signals),
                "waiting_up_to_seconds": timeout,
            },
            level="minimal",
        )

    started = time.monotonic()
    next_update_at = started + queue_update_seconds
    last_state = state

    while time.monotonic() - started < timeout:
        time.sleep(poll_seconds)
        last_state = detect_queue_page(driver, site)
        if not last_state.in_queue:
            waited = time.monotonic() - started
            logger.info("[queue][%s] cleared phase=%s waited=%.1fs url=%s", site, phase, waited, last_state.url)
            if trace is not None:
                trace.step(
                    "Queue cleared",
                    {"phase": phase, "url": last_state.url, "waited_seconds": round(waited, 1)},
                )
            return last_state

        now = time.monotonic()
        if now >= next_update_at:
            if trace is not None:
                trace.warning(
                    "Still waiting in queue",
                    {
                        "phase": phase,
                        "url": last_state.url,
                        "elapsed_seconds": round(now - started, 1),
                        "timeout_seconds": timeout,
                    },
                    level="normal",
                )
            next_update_at = now + queue_update_seconds

    waited = time.monotonic() - started
    if trace is not None:
        trace.error(
            "Queue timeout",
            {
                "phase": phase,
                "url": last_state.url,
                "elapsed_seconds": round(waited, 1),
                "timeout_seconds": timeout,
            },
        )
    raise QueueTimeoutError(
        f"{site or 'worker'}: queue timeout phase={phase} timeout={timeout:.1f}s url={last_state.url}"
    )
