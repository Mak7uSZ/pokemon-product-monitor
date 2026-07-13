from __future__ import annotations

import enum
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional


class BreakerState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BackoffPolicy:
    """
    Exponential backoff with additive jitter.
    Example:
        attempt=1 -> base
        attempt=2 -> base * factor
        attempt=3 -> base * factor^2
    capped by max_seconds
    """
    base_seconds: float
    max_seconds: float
    jitter_seconds: float = 0.0
    factor: float = 2.0

    def compute(self, consecutive_denies: int) -> float:
        if consecutive_denies <= 0:
            raw = self.base_seconds
        else:
            raw = self.base_seconds * (self.factor ** max(0, consecutive_denies - 1))
        raw = min(raw, self.max_seconds)

        if self.jitter_seconds > 0:
            raw += random.uniform(0.0, self.jitter_seconds)

        return raw


@dataclass(frozen=True)
class LayerPolicy:
    """
    Policy for a specific layer: parser or worker.
    """
    min_interval_seconds: float
    open_after_denies: int
    backoff: BackoffPolicy
    half_open_probe_allowed: int = 1
    job_timeout_seconds: Optional[float] = None


@dataclass(frozen=True)
class SitePolicy:
    site: str
    parser: LayerPolicy
    worker: LayerPolicy


@dataclass
class LayerRuntimeState:
    """
    Mutable state for one layer of one site.
    """
    last_op_at: float = 0.0
    cooldown_until: float = 0.0

    consecutive_denies: int = 0
    consecutive_successes: int = 0
    last_deny_kind: Optional[str] = None
    last_success_at: float = 0.0

    breaker: BreakerState = BreakerState.CLOSED
    half_open_probes_used: int = 0

    last_deny_at: float = 0.0


@dataclass
class SiteRuntimeState:
    parser: LayerRuntimeState = field(default_factory=LayerRuntimeState)
    worker: LayerRuntimeState = field(default_factory=LayerRuntimeState)


class AntiBanManager:
    """
    Central anti-ban state machine for all sites.

    Responsibilities:
    - per-site / per-layer allow checks
    - client-side minimum intervals
    - cooldown windows after denies
    - circuit breaker OPEN -> HALF_OPEN -> CLOSED
    - deny/success accounting
    """

    def __init__(self, policies: dict[str, SitePolicy]) -> None:
        self.policies = policies
        self._lock = threading.RLock()
        self.state: dict[str, SiteRuntimeState] = {
            site: SiteRuntimeState() for site in policies.keys()
        }

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _layer_policy(self, site_policy: SitePolicy, layer: str) -> LayerPolicy:
        if layer == "parser":
            return site_policy.parser
        if layer == "worker":
            return site_policy.worker
        raise ValueError(f"unknown layer={layer}")

    def _layer_state(self, site_state: SiteRuntimeState, layer: str) -> LayerRuntimeState:
        if layer == "parser":
            return site_state.parser
        if layer == "worker":
            return site_state.worker
        raise ValueError(f"unknown layer={layer}")

    def allow(self, site: str, layer: str) -> tuple[bool, str]:
        """
        Returns:
            (True, "ok")
            (True, "half_open_probe")
            (False, "cooldown ...")
            (False, "rate_limited_by_client")
            (False, "half_open_no_probe_budget")
        """
        if site not in self.policies:
            return True, "no_policy"

        with self._lock:
            now = self._now()
            site_policy = self.policies[site]
            site_state = self.state[site]

            layer_policy = self._layer_policy(site_policy, layer)
            layer_state = self._layer_state(site_state, layer)

            if now < layer_state.cooldown_until:
                return False, f"cooldown {layer_state.cooldown_until - now:.1f}s"

            if (
                layer_state.last_op_at > 0
                and now - layer_state.last_op_at < layer_policy.min_interval_seconds
            ):
                return False, "rate_limited_by_client"

            if layer_state.breaker == BreakerState.OPEN:
                layer_state.breaker = BreakerState.HALF_OPEN
                layer_state.half_open_probes_used = 0

            if layer_state.breaker == BreakerState.HALF_OPEN:
                if layer_state.half_open_probes_used >= layer_policy.half_open_probe_allowed:
                    return False, "half_open_no_probe_budget"
                return True, "half_open_probe"

            return True, "ok"

    def mark_op(self, site: str, layer: str) -> None:
        if site not in self.policies:
            return
        with self._lock:
            layer_state = self._layer_state(self.state[site], layer)
            layer_state.last_op_at = self._now()

    def use_half_open_probe(self, site: str, layer: str) -> None:
        if site not in self.policies:
            return
        with self._lock:
            layer_state = self._layer_state(self.state[site], layer)
            if layer_state.breaker == BreakerState.HALF_OPEN:
                layer_state.half_open_probes_used += 1

    def report_success(self, site: str, layer: str) -> None:
        if site not in self.policies:
            return

        with self._lock:
            now = self._now()
            layer_state = self._layer_state(self.state[site], layer)

            layer_state.consecutive_successes += 1
            layer_state.last_success_at = now

            # Successful probe or successful normal traffic closes the breaker.
            layer_state.breaker = BreakerState.CLOSED
            layer_state.consecutive_denies = 0
            layer_state.last_deny_kind = None
            layer_state.half_open_probes_used = 0
            layer_state.cooldown_until = 0.0

    def report_deny(
        self,
        site: str,
        layer: str,
        deny_kind: str,
        retry_after_seconds: Optional[float] = None,
    ) -> float:
        """
        Records deny and returns applied cooldown seconds.
        """
        if site not in self.policies:
            return 0.0

        with self._lock:
            now = self._now()
            site_policy = self.policies[site]
            layer_policy = self._layer_policy(site_policy, layer)
            layer_state = self._layer_state(self.state[site], layer)

            layer_state.consecutive_denies += 1
            layer_state.consecutive_successes = 0
            layer_state.last_deny_kind = deny_kind
            layer_state.last_deny_at = now

            computed = layer_policy.backoff.compute(layer_state.consecutive_denies)
            if retry_after_seconds is not None:
                computed = max(computed, float(retry_after_seconds))

            layer_state.cooldown_until = max(layer_state.cooldown_until, now + computed)

            if layer_state.consecutive_denies >= layer_policy.open_after_denies:
                layer_state.breaker = BreakerState.OPEN
            else:
                layer_state.breaker = BreakerState.CLOSED

            layer_state.half_open_probes_used = 0
            return computed

    def get_job_timeout_seconds(self, site: str) -> Optional[float]:
        if site not in self.policies:
            return None
        with self._lock:
            return self.policies[site].worker.job_timeout_seconds

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            out: dict[str, dict] = {}
            for site, site_state in self.state.items():
                out[site] = {
                    "parser": asdict(site_state.parser),
                    "worker": asdict(site_state.worker),
                }
            return out
