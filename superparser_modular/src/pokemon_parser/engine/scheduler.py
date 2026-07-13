from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from pokemon_parser.config import AppConfig
from pokemon_parser.engine.antiban import AntiBanManager


def site_interval(cfg: AppConfig, site: str) -> float:
    """
    Base scan interval per site with jitter.
    Scan settings can explicitly override legacy per-site/turbo pacing.
    """
    base = cfg.effective_scan_delay_seconds(site)
    jitter = max(0.0, float(getattr(cfg, "jitter", 0.0)))
    interval = max(0.1, float(base) + random.uniform(-jitter, jitter))
    return max(0.1, interval)


@dataclass
class SiteScheduleState:
    site: str
    next_run_at: float = 0.0
    last_run_at: float = 0.0
    last_delay_seconds: float = 0.0
    runs: int = 0


@dataclass
class Scheduler:
    cfg: AppConfig
    sites: list[str]
    states: dict[str, SiteScheduleState] = field(init=False)

    def __post_init__(self) -> None:
        now = time.monotonic()
        self.states = {
            site: SiteScheduleState(site=site, next_run_at=now)
            for site in self.sites
        }

    def pick_next_site(self) -> str:
        """
        Pick the site with the earliest scheduled next run.
        """
        return min(
            self.states.values(),
            key=lambda st: (st.next_run_at, st.site),
        ).site

    def sleep_needed(self, site: str) -> float:
        """
        Returns how many seconds remain before this site should run.
        """
        now = time.monotonic()
        st = self.states[site]
        return max(0.0, st.next_run_at - now)

    def mark_run_started(self, site: str) -> None:
        now = time.monotonic()
        st = self.states[site]
        st.last_run_at = now
        st.runs += 1

    def mark_run_finished(
        self,
        site: str,
        antiban: AntiBanManager | None = None,
    ) -> float:
        """
        Compute and store the next delay for a site after its scan has finished.

        Delay sources:
        1. Base site interval (turbo/low + jitter)
        2. Parser-side antiban cooldown if present
        3. Small half-open / breaker-aware penalty if parser layer is not healthy
        """
        now = time.monotonic()
        st = self.states[site]

        delay = site_interval(self.cfg, site)

        if antiban is not None and site in antiban.policies:
            parser_state = antiban.state[site].parser
            cooldown_remaining = max(0.0, parser_state.cooldown_until - now)
            if cooldown_remaining > 0:
                delay = max(delay, cooldown_remaining)

            # If breaker is not fully closed, bias toward a slower pace.
            if str(parser_state.breaker) != "BreakerState.CLOSED" and getattr(parser_state.breaker, "value", "") != "closed":
                delay = max(delay, delay * 1.5)

        st.last_delay_seconds = delay
        st.next_run_at = now + delay
        return delay

    def force_delay(self, site: str, delay_seconds: float) -> None:
        """
        Manually push a site into the future.
        Useful for explicit runtime interventions.
        """
        now = time.monotonic()
        st = self.states[site]
        delay = max(0.0, float(delay_seconds))
        st.last_delay_seconds = delay
        st.next_run_at = now + delay

    def snapshot(self) -> dict[str, dict]:
        now = time.monotonic()
        out: dict[str, dict] = {}
        for site, st in self.states.items():
            out[site] = {
                "next_in_seconds": round(max(0.0, st.next_run_at - now), 3),
                "last_run_at": round(st.last_run_at, 3),
                "last_delay_seconds": round(st.last_delay_seconds, 3),
                "runs": st.runs,
            }
        return out
