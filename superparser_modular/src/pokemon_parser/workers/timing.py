from __future__ import annotations

from dataclasses import dataclass

from pokemon_parser.config import AppConfig


@dataclass(frozen=True)
class WorkerTiming:
    click_pause_seconds: float
    after_navigation_wait_seconds: float
    after_add_to_cart_wait_seconds: float
    after_checkout_click_wait_seconds: float
    wait_timeout_seconds: float
    poll_seconds: float
    retry_pause_seconds: float


PROFILE_DEFAULTS: dict[str, WorkerTiming] = {
    "safe": WorkerTiming(
        click_pause_seconds=0.35,
        after_navigation_wait_seconds=1.0,
        after_add_to_cart_wait_seconds=1.2,
        after_checkout_click_wait_seconds=1.2,
        wait_timeout_seconds=25.0,
        poll_seconds=0.25,
        retry_pause_seconds=0.6,
    ),
    "balanced": WorkerTiming(
        click_pause_seconds=0.2,
        after_navigation_wait_seconds=0.5,
        after_add_to_cart_wait_seconds=0.6,
        after_checkout_click_wait_seconds=0.6,
        wait_timeout_seconds=20.0,
        poll_seconds=0.2,
        retry_pause_seconds=0.45,
    ),
    "fast": WorkerTiming(
        click_pause_seconds=0.08,
        after_navigation_wait_seconds=0.2,
        after_add_to_cart_wait_seconds=0.25,
        after_checkout_click_wait_seconds=0.25,
        wait_timeout_seconds=15.0,
        poll_seconds=0.12,
        retry_pause_seconds=0.2,
    ),
}


def build_worker_timing(cfg: AppConfig) -> WorkerTiming:
    profile = (cfg.worker_speed_profile or "balanced").strip().lower()
    base = PROFILE_DEFAULTS.get(profile, PROFILE_DEFAULTS["balanced"])

    if profile != "custom":
        return base

    return WorkerTiming(
        click_pause_seconds=max(0.0, float(cfg.worker_click_pause_seconds)),
        after_navigation_wait_seconds=max(0.0, float(cfg.worker_after_navigation_wait_seconds)),
        after_add_to_cart_wait_seconds=max(0.0, float(cfg.worker_after_add_to_cart_wait_seconds)),
        after_checkout_click_wait_seconds=max(0.0, float(cfg.worker_after_checkout_click_wait_seconds)),
        wait_timeout_seconds=max(1.0, float(cfg.worker_wait_timeout_seconds)),
        poll_seconds=max(0.05, float(cfg.worker_poll_seconds)),
        retry_pause_seconds=max(0.0, float(cfg.worker_retry_pause_seconds)),
    )
