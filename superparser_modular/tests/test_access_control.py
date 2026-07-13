from pokemon_parser.engine.access_control import (
    AccessAssessment,
    AccessOutcome,
    AccessSeverity,
    RecoveryAction,
    SourceAccessController,
    SourceAccessPolicy,
    SourceAccessState,
    detect_challenge,
)


def test_detector_requires_explicit_evidence_for_bare_403():
    detection = detect_challenge(status_code=403, html="<html><h1>Forbidden</h1></html>")
    assert detection.detected is False


def test_detector_recognizes_explicit_captcha_and_bot_check_signals():
    captcha = detect_challenge(html='<div class="g-recaptcha" data-sitekey="redacted"></div>')
    bot_check = detect_challenge(title="Verify you are human")

    assert captcha.detected is True
    assert captcha.reason_code == "challenge_captcha"
    assert captcha.evidence == ("dom_captcha_marker",)
    assert bot_check.detected is True
    assert bot_check.reason_code == "challenge_bot_check"


def test_detector_recognizes_are_you_a_bot_and_access_denied_pages():
    bot_page = detect_challenge(text="Are you a bot?")
    denied_page = detect_challenge(html="<html><h1>Access denied</h1></html>")

    assert bot_page.detected is True
    assert bot_page.reason_code == "challenge_bot_check"
    assert denied_page.detected is True
    assert denied_page.reason_code == "challenge_access_denied"


def test_detector_does_not_mistake_normal_error_page_for_challenge():
    detection = detect_challenge(
        status_code=500,
        title="Something went wrong",
        text="The product service is temporarily unavailable. Try again later.",
    )
    assert detection.detected is False


def test_detector_classifies_explicit_rate_limit_page():
    detection = detect_challenge(status_code=429, text="429 Too Many Requests")
    assert detection.detected is True
    assert detection.reason_code == "challenge_rate_limited"
    assert detection.recommended_action == RecoveryAction.COOLDOWN


def test_source_state_escalates_repeated_soft_denies_then_resets_on_success():
    now = [100.0]
    controller = SourceAccessController(clock=lambda: now[0], random_fn=lambda: 0.0)
    policy = SourceAccessPolicy(
        soft_escalation_threshold=3,
        observation_window_seconds=60.0,
        base_cooldown_seconds=10.0,
        max_cooldown_seconds=30.0,
        jitter_ratio=0.0,
    )
    soft = AccessAssessment(
        AccessOutcome.SOFT_DENY,
        "ambiguous_403",
        AccessSeverity.SOFT,
        status_code=403,
    )

    first = controller.observe("mediamarkt.graphql", soft, policy)
    second = controller.observe("mediamarkt.graphql", soft, policy)
    third = controller.observe("mediamarkt.graphql", soft, policy)

    assert first.action == RecoveryAction.FALLBACK
    assert second.action == RecoveryAction.FALLBACK
    assert third.action == RecoveryAction.COOLDOWN
    assert third.escalated is True
    assert third.cooldown_seconds == 10.0
    assert controller.allow("mediamarkt.graphql") == (False, "cooling_down")

    now[0] = 111.0
    assert controller.allow("mediamarkt.graphql") == (True, "probe")
    recovered = controller.observe(
        "mediamarkt.graphql",
        AccessAssessment(AccessOutcome.SUCCESS, "probe_success"),
        policy,
    )
    assert recovered.state == SourceAccessState.RECOVERED
    snapshot = controller.snapshot("mediamarkt.graphql")
    assert snapshot["soft_occurrences"] == 0
    assert snapshot["consecutive_cooldowns"] == 1
    assert snapshot["cooldown_until_epoch"] is None

    stable = controller.observe(
        "mediamarkt.graphql",
        AccessAssessment(AccessOutcome.SUCCESS, "stable_success"),
        policy,
    )
    assert stable.state == SourceAccessState.NORMAL
    assert controller.snapshot("mediamarkt.graphql")["consecutive_cooldowns"] == 0


def test_soft_deny_observation_window_is_bounded():
    now = [0.0]
    controller = SourceAccessController(clock=lambda: now[0], random_fn=lambda: 0.0)
    policy = SourceAccessPolicy(
        soft_escalation_threshold=2,
        observation_window_seconds=5.0,
        base_cooldown_seconds=10.0,
        jitter_ratio=0.0,
    )
    soft = AccessAssessment(AccessOutcome.SOFT_DENY, "ambiguous", AccessSeverity.SOFT)

    controller.observe("source", soft, policy)
    now[0] = 6.0
    decision = controller.observe("source", soft, policy)

    assert decision.action == RecoveryAction.FALLBACK
    assert decision.occurrence_count == 1
