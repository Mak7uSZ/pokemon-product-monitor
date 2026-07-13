from __future__ import annotations

import enum
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


class AccessOutcome(str, enum.Enum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    VALID_EMPTY = "valid_empty"
    TRANSIENT_FAILURE = "transient_failure"
    SOFT_DENY = "soft_deny"
    STRONG_DENY = "strong_deny"
    PARSE_FAILURE = "parse_failure"


class AccessSeverity(str, enum.Enum):
    NONE = "none"
    DEGRADED = "degraded"
    SOFT = "soft"
    STRONG = "strong"


class ChallengeKind(str, enum.Enum):
    CAPTCHA = "captcha"
    BOT_CHECK = "bot_check"
    ACCESS_DENIED = "access_denied"
    RATE_LIMITED = "rate_limited"
    SECURITY_INTERSTITIAL = "security_interstitial"
    UNKNOWN_CHALLENGE = "unknown_challenge"


class SourceAccessState(str, enum.Enum):
    NORMAL = "normal"
    SUSPECTED_CHALLENGE = "suspected_challenge"
    CHALLENGED = "challenged"
    COOLING_DOWN = "cooling_down"
    PROBING = "probing"
    RECOVERED = "recovered"
    MANUALLY_BLOCKED = "manually_blocked"


class RecoveryAction(str, enum.Enum):
    CONTINUE = "continue"
    RETRY = "retry"
    FALLBACK = "fallback"
    COOLDOWN = "cooldown"
    PAUSE_SOURCE = "pause_source"
    MANUAL_ACTION = "manual_action"


@dataclass(frozen=True)
class ChallengeDetection:
    detected: bool
    kind: ChallengeKind | None = None
    reason_code: str | None = None
    confidence: str = "none"
    evidence: tuple[str, ...] = ()
    source: str | None = None
    recommended_action: RecoveryAction = RecoveryAction.CONTINUE


@dataclass(frozen=True)
class AccessAssessment:
    outcome: AccessOutcome
    reason_code: str
    severity: AccessSeverity = AccessSeverity.NONE
    status_code: int | None = None
    retryable: bool = False
    payload: dict[str, Any] | None = None
    error_count: int = 0
    challenge: ChallengeDetection | None = None


@dataclass(frozen=True)
class SourceAccessPolicy:
    soft_escalation_threshold: int = 3
    observation_window_seconds: float = 300.0
    base_cooldown_seconds: float = 5.0
    cooldown_multiplier: float = 2.0
    max_cooldown_seconds: float = 300.0
    jitter_ratio: float = 0.1


@dataclass(frozen=True)
class RecoveryDecision:
    source: str
    state: SourceAccessState
    action: RecoveryAction
    reason_code: str
    cooldown_seconds: float = 0.0
    cooldown_until: float = 0.0
    occurrence_count: int = 0
    escalated: bool = False


@dataclass
class _SourceState:
    state: SourceAccessState = SourceAccessState.NORMAL
    soft_events: deque[float] = field(default_factory=deque)
    soft_reason_code: str | None = None
    cooldown_until: float = 0.0
    consecutive_cooldowns: int = 0
    last_reason_code: str | None = None
    last_outcome: AccessOutcome | None = None


def detect_challenge(
    *,
    url: str | None = None,
    title: str | None = None,
    text: str | None = None,
    html: str | None = None,
    status_code: int | None = None,
    source: str | None = None,
) -> ChallengeDetection:
    """Detect explicit challenge evidence without treating an HTTP status alone as proof."""

    url_lower = (url or "").lower()
    title_lower = (title or "").lower()
    body_lower = " ".join(part for part in ((text or ""), (html or "")) if part).lower()[:200_000]
    combined = f"{title_lower}\n{body_lower}"

    checks: tuple[tuple[ChallengeKind, str, tuple[str, ...], str], ...] = (
        (
            ChallengeKind.CAPTCHA,
            "challenge_captcha",
            ("g-recaptcha", "h-captcha", "recaptcha/api", "captcha-container"),
            "dom_captcha_marker",
        ),
        (
            ChallengeKind.BOT_CHECK,
            "challenge_bot_check",
            (
                "verify you are human",
                "prove you are human",
                "are you a bot",
                "controleer of je een mens bent",
                "unusual traffic from your computer network",
            ),
            "bot_check_phrase",
        ),
        (
            ChallengeKind.ACCESS_DENIED,
            "challenge_access_denied",
            ("you have been blocked", "request blocked", "access denied"),
            "access_denied_phrase",
        ),
        (
            ChallengeKind.SECURITY_INTERSTITIAL,
            "challenge_security_interstitial",
            ("cf-chl-", "challenge-platform", "cdn-cgi/challenge-platform"),
            "security_interstitial_marker",
        ),
    )
    for kind, reason, markers, evidence_name in checks:
        if any(marker in combined for marker in markers):
            return ChallengeDetection(
                True,
                kind,
                reason,
                "high",
                (evidence_name,),
                source,
                RecoveryAction.MANUAL_ACTION,
            )

    url_markers = ("/captcha", "/challenge", "/sorry/index", "/cdn-cgi/challenge")
    if any(marker in url_lower for marker in url_markers):
        return ChallengeDetection(
            True,
            ChallengeKind.SECURITY_INTERSTITIAL,
            "challenge_url_interstitial",
            "high",
            ("challenge_url_marker",),
            source,
            RecoveryAction.PAUSE_SOURCE,
        )

    if status_code == 429 or any(
        phrase in combined for phrase in ("429 too many requests", "rate limit exceeded", "too many requests")
    ):
        return ChallengeDetection(
            True,
            ChallengeKind.RATE_LIMITED,
            "challenge_rate_limited",
            "high" if "too many requests" in combined else "medium",
            ("rate_limit_signal",),
            source,
            RecoveryAction.COOLDOWN,
        )

    # A bare 403 can be a routing, policy, or endpoint mismatch. It is deliberately
    # not considered a confirmed challenge without one of the explicit signals above.
    return ChallengeDetection(False, source=source)


class SourceAccessController:
    """Small source-scoped recovery state machine shared by HTTP and browser channels."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self._clock = clock
        self._random = random_fn
        self._sources: dict[str, _SourceState] = {}

    def _entry(self, source: str) -> _SourceState:
        return self._sources.setdefault(source, _SourceState())

    @staticmethod
    def _bounded_policy(policy: SourceAccessPolicy) -> SourceAccessPolicy:
        return SourceAccessPolicy(
            soft_escalation_threshold=max(1, int(policy.soft_escalation_threshold)),
            observation_window_seconds=max(1.0, float(policy.observation_window_seconds)),
            base_cooldown_seconds=max(0.0, float(policy.base_cooldown_seconds)),
            cooldown_multiplier=max(1.0, float(policy.cooldown_multiplier)),
            max_cooldown_seconds=max(0.0, float(policy.max_cooldown_seconds)),
            jitter_ratio=min(0.5, max(0.0, float(policy.jitter_ratio))),
        )

    def allow(self, source: str) -> tuple[bool, str]:
        entry = self._entry(source)
        now = self._clock()
        if entry.state == SourceAccessState.MANUALLY_BLOCKED:
            return False, "manually_blocked"
        if entry.cooldown_until > now:
            entry.state = SourceAccessState.COOLING_DOWN
            return False, "cooling_down"
        if entry.cooldown_until:
            entry.cooldown_until = 0.0
            entry.state = SourceAccessState.PROBING
            return True, "probe"
        return True, entry.state.value

    def _cooldown(
        self,
        source: str,
        entry: _SourceState,
        assessment: AccessAssessment,
        policy: SourceAccessPolicy,
        *,
        action: RecoveryAction,
        occurrence_count: int,
        escalated: bool,
        retry_after_seconds: float | None,
    ) -> RecoveryDecision:
        entry.consecutive_cooldowns += 1
        base = policy.base_cooldown_seconds * (
            policy.cooldown_multiplier ** max(0, entry.consecutive_cooldowns - 1)
        )
        bounded = min(policy.max_cooldown_seconds, base)
        jitter = bounded * policy.jitter_ratio * self._random()
        cooldown = min(policy.max_cooldown_seconds, bounded + jitter)
        if retry_after_seconds is not None:
            cooldown = min(policy.max_cooldown_seconds, max(cooldown, max(0.0, retry_after_seconds)))
        entry.cooldown_until = self._clock() + cooldown
        entry.state = SourceAccessState.COOLING_DOWN
        return RecoveryDecision(
            source=source,
            state=entry.state,
            action=action,
            reason_code=assessment.reason_code,
            cooldown_seconds=cooldown,
            cooldown_until=entry.cooldown_until,
            occurrence_count=occurrence_count,
            escalated=escalated,
        )

    def observe(
        self,
        source: str,
        assessment: AccessAssessment,
        policy: SourceAccessPolicy,
        *,
        retry_after_seconds: float | None = None,
    ) -> RecoveryDecision:
        policy = self._bounded_policy(policy)
        entry = self._entry(source)
        now = self._clock()
        entry.last_reason_code = assessment.reason_code
        entry.last_outcome = assessment.outcome

        if assessment.outcome in {
            AccessOutcome.SUCCESS,
            AccessOutcome.PARTIAL_SUCCESS,
            AccessOutcome.VALID_EMPTY,
        }:
            previous_state = entry.state
            had_failure_state = previous_state not in {
                SourceAccessState.NORMAL,
                SourceAccessState.RECOVERED,
            } or bool(entry.soft_events)
            entry.soft_events.clear()
            entry.soft_reason_code = None
            entry.cooldown_until = 0.0
            if previous_state == SourceAccessState.RECOVERED:
                entry.consecutive_cooldowns = 0
                entry.state = SourceAccessState.NORMAL
            else:
                entry.state = SourceAccessState.RECOVERED if had_failure_state else SourceAccessState.NORMAL
            return RecoveryDecision(
                source,
                entry.state,
                RecoveryAction.CONTINUE,
                assessment.reason_code,
            )

        if assessment.outcome == AccessOutcome.STRONG_DENY:
            entry.state = SourceAccessState.CHALLENGED
            return self._cooldown(
                source,
                entry,
                assessment,
                policy,
                action=RecoveryAction.PAUSE_SOURCE,
                occurrence_count=1,
                escalated=False,
                retry_after_seconds=retry_after_seconds,
            )

        if assessment.outcome == AccessOutcome.SOFT_DENY:
            if entry.soft_reason_code not in {None, assessment.reason_code}:
                entry.soft_events.clear()
            entry.soft_reason_code = assessment.reason_code
            while entry.soft_events and now - entry.soft_events[0] > policy.observation_window_seconds:
                entry.soft_events.popleft()
            entry.soft_events.append(now)
            count = len(entry.soft_events)
            if count >= policy.soft_escalation_threshold:
                return self._cooldown(
                    source,
                    entry,
                    assessment,
                    policy,
                    action=RecoveryAction.COOLDOWN,
                    occurrence_count=count,
                    escalated=True,
                    retry_after_seconds=retry_after_seconds,
                )
            entry.state = SourceAccessState.SUSPECTED_CHALLENGE
            return RecoveryDecision(
                source,
                entry.state,
                RecoveryAction.FALLBACK,
                assessment.reason_code,
                occurrence_count=count,
            )

        if assessment.outcome == AccessOutcome.TRANSIENT_FAILURE:
            entry.state = SourceAccessState.SUSPECTED_CHALLENGE
            if assessment.status_code == 429:
                return self._cooldown(
                    source,
                    entry,
                    assessment,
                    policy,
                    action=RecoveryAction.COOLDOWN,
                    occurrence_count=1,
                    escalated=False,
                    retry_after_seconds=retry_after_seconds,
                )
            return RecoveryDecision(
                source,
                entry.state,
                RecoveryAction.RETRY if assessment.retryable else RecoveryAction.FALLBACK,
                assessment.reason_code,
                occurrence_count=1,
            )

        entry.state = SourceAccessState.SUSPECTED_CHALLENGE
        return RecoveryDecision(
            source,
            entry.state,
            RecoveryAction.FALLBACK,
            assessment.reason_code,
            occurrence_count=1,
        )

    def snapshot(self, source: str) -> dict[str, Any]:
        entry = self._entry(source)
        now = self._clock()
        return {
            "source": source,
            "state": entry.state.value,
            "cooldown_until_epoch": entry.cooldown_until if entry.cooldown_until > now else None,
            "cooldown_remaining_seconds": max(0.0, entry.cooldown_until - now),
            "soft_occurrences": len(entry.soft_events),
            "soft_reason_code": entry.soft_reason_code,
            "consecutive_cooldowns": entry.consecutive_cooldowns,
            "last_reason_code": entry.last_reason_code,
            "last_outcome": entry.last_outcome.value if entry.last_outcome else None,
        }

    def reset(self, source: str) -> None:
        self._sources[source] = _SourceState(state=SourceAccessState.RECOVERED)
