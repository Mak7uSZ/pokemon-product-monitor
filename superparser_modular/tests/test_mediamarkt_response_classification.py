import asyncio
import json
import logging

import pytest

from pokemon_parser.engine.access_control import AccessAssessment, AccessOutcome, AccessSeverity
from pokemon_parser.parsers.mediamarkt import MediaMarktParser, MediaMarktParserDeny


PRODUCT = {
    "id": "1895844",
    "title": "Pokemon Ascended Heroes ETB",
    "url": "/nl/product/_pokemon-1895844.html",
    "price": 79.99,
    "inStock": True,
}


class _Cfg:
    def max_retries(self):
        return 0

    def retry_delay_seconds(self):
        return 0.0

    def site_request_timeout_seconds(self, site):
        return 3.0

    def mediamarkt_graphql_backoff_seconds(self):
        return 5.0

    def mediamarkt_graphql_backoff_multiplier(self):
        return 2.0

    def mediamarkt_graphql_max_backoff_seconds(self):
        return 30.0

    def mediamarkt_graphql_soft_deny_escalation_threshold(self):
        return 3

    def mediamarkt_graphql_soft_deny_window_seconds(self):
        return 60.0


@pytest.mark.parametrize(
    ("status", "body", "outcome", "reason"),
    [
        (200, '{"data":{"category":{"products":[]}}}', AccessOutcome.VALID_EMPTY, "graphql_valid_empty_result"),
        (200, "not-json", AccessOutcome.PARSE_FAILURE, "graphql_malformed_json"),
        (200, "", AccessOutcome.TRANSIENT_FAILURE, "graphql_incomplete_empty_response"),
        (
            200,
            '{"data":null,"errors":[{"message":"Forbidden","extensions":{"code":"FORBIDDEN"}}]}',
            AccessOutcome.SOFT_DENY,
            "graphql_error_without_usable_data",
        ),
        (
            200,
            '{"data":null,"errors":[{"message":"PersistedQueryNotFound"}]}',
            AccessOutcome.PARSE_FAILURE,
            "graphql_schema_or_persisted_query_mismatch",
        ),
        (403, "Forbidden", AccessOutcome.SOFT_DENY, "graphql_http_403_ambiguous"),
        (403, "<html>Verify you are human</html>", AccessOutcome.STRONG_DENY, "challenge_bot_check"),
        (429, "Too many requests", AccessOutcome.TRANSIENT_FAILURE, "graphql_http_429"),
        (500, "Service unavailable", AccessOutcome.TRANSIENT_FAILURE, "graphql_http_500"),
        (502, "Service unavailable", AccessOutcome.TRANSIENT_FAILURE, "graphql_http_502"),
        (503, "Service unavailable", AccessOutcome.TRANSIENT_FAILURE, "graphql_http_503"),
    ],
)
def test_graphql_response_classes_are_explicit(status, body, outcome, reason):
    assessment = MediaMarktParser()._classify_graphql_response(status=status, body=body)
    assert assessment.outcome == outcome
    assert assessment.reason_code == reason


def test_graphql_success_and_partial_success_keep_usable_data():
    parser = MediaMarktParser()
    success = parser._classify_graphql_response(
        status=200,
        body=json.dumps({"data": {"category": {"products": [PRODUCT]}}}),
    )
    partial = parser._classify_graphql_response(
        status=200,
        body=json.dumps(
            {
                "data": {"category": {"products": [PRODUCT]}},
                "errors": [{"message": "secondary resolver failed"}],
            }
        ),
    )

    assert success.outcome == AccessOutcome.SUCCESS
    assert success.payload is not None
    assert partial.outcome == AccessOutcome.PARTIAL_SUCCESS
    assert partial.payload is not None
    assert partial.error_count == 1


def test_graphql_timeout_is_a_retryable_transient_failure():
    parser = MediaMarktParser()

    class TimeoutSession:
        def get(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    assessment = asyncio.run(parser._fetch_graphql_page(TimeoutSession(), _Cfg(), 1))
    assert assessment.outcome == AccessOutcome.TRANSIENT_FAILURE
    assert assessment.reason_code == "graphql_transport_timeouterror"
    assert assessment.retryable is True


def test_single_ambiguous_403_uses_html_fallback_without_opening_circuit(monkeypatch, caplog):
    parser = MediaMarktParser()
    secret_body = "do-not-log-response-body"

    async def ambiguous_403(session, cfg, page):
        assessment = parser._classify_graphql_response(status=403, body=secret_body)
        parser._record_graphql_assessment(assessment, page=page)
        return assessment

    async def useful_html(session, cfg, url):
        return '<a href="/nl/product/_pokemon-1895844.html">Pokemon Ascended Heroes ETB</a>'

    monkeypatch.setattr(parser, "_fetch_graphql_page", ambiguous_403)
    monkeypatch.setattr(parser, "_fetch_html", useful_html)
    caplog.set_level(logging.INFO)

    sources = asyncio.run(
        parser._fetch_page_sources(object(), _Cfg(), 1, "https://www.mediamarkt.nl/category")
    )

    assert sources["graphql_data"] is None
    assert sources["html"] is not None
    assert sources["soft_graphql_deny"] is True
    assert parser.graphql_circuit_snapshot()["graphql_circuit_open"] is False
    assert secret_body not in caplog.text


def test_repeated_ambiguous_403_escalates_only_endpoint_circuit(monkeypatch):
    parser = MediaMarktParser()
    graphql_calls = {"count": 0}

    async def ambiguous_403(session, cfg, page):
        graphql_calls["count"] += 1
        return AccessAssessment(
            AccessOutcome.SOFT_DENY,
            "graphql_http_403_ambiguous",
            AccessSeverity.SOFT,
            status_code=403,
        )

    async def useful_html(session, cfg, url):
        return '<a href="/nl/product/_pokemon-1895844.html">Pokemon Ascended Heroes ETB</a>'

    monkeypatch.setattr(parser, "_fetch_graphql_page", ambiguous_403)
    monkeypatch.setattr(parser, "_fetch_html", useful_html)

    for page in (1, 2, 3):
        asyncio.run(parser._fetch_page_sources(object(), _Cfg(), page, f"https://example.test/{page}"))

    assert graphql_calls["count"] == 3
    snapshot = parser.graphql_circuit_snapshot()
    assert snapshot["graphql_circuit_open"] is True
    assert snapshot["graphql_soft_deny_occurrences"] == 3

    asyncio.run(parser._fetch_page_sources(object(), _Cfg(), 4, "https://example.test/4"))
    assert graphql_calls["count"] == 3


def test_success_resets_soft_deny_streak(monkeypatch):
    parser = MediaMarktParser()
    results = iter(
        (
            AccessAssessment(
                AccessOutcome.SOFT_DENY,
                "graphql_http_403_ambiguous",
                AccessSeverity.SOFT,
                status_code=403,
            ),
            AccessAssessment(
                AccessOutcome.VALID_EMPTY,
                "graphql_valid_empty_result",
                payload={"data": {"category": {"products": []}}},
                status_code=200,
            ),
        )
    )

    async def next_result(session, cfg, page):
        return next(results)

    async def useful_html(session, cfg, url):
        return '<a href="/nl/product/_pokemon-1895844.html">Pokemon</a>'

    monkeypatch.setattr(parser, "_fetch_graphql_page", next_result)
    monkeypatch.setattr(parser, "_fetch_html", useful_html)

    asyncio.run(parser._fetch_page_sources(object(), _Cfg(), 1, "https://example.test/1"))
    asyncio.run(parser._fetch_page_sources(object(), _Cfg(), 2, "https://example.test/2"))

    snapshot = parser.graphql_circuit_snapshot()
    assert snapshot["graphql_soft_deny_occurrences"] == 0
    assert snapshot["graphql_circuit_open"] is False
    assert snapshot["graphql_access_state"] == "recovered"


def test_explicit_graphql_challenge_raises_strong_parser_deny(monkeypatch):
    parser = MediaMarktParser()

    async def challenge(session, cfg, page):
        return AccessAssessment(
            AccessOutcome.STRONG_DENY,
            "challenge_captcha",
            AccessSeverity.STRONG,
            status_code=403,
        )

    monkeypatch.setattr(parser, "_fetch_graphql_page", challenge)

    with pytest.raises(MediaMarktParserDeny) as exc_info:
        asyncio.run(parser._fetch_page_sources(object(), _Cfg(), 1, "https://example.test"))
    assert exc_info.value.message == "challenge_captcha"
