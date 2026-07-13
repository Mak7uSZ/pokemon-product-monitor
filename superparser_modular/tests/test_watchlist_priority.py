import asyncio
import json
import queue
import sqlite3
from pathlib import Path

import aiohttp

from pokemon_parser.engine.pipeline import Pipeline
from pokemon_parser.engine.antiban import AntiBanManager, BackoffPolicy, LayerPolicy, SitePolicy
import pokemon_parser.api.routes.watchlist as watchlist_routes
from pokemon_parser.engine.selenium_dispatcher import SeleniumDispatcher
from pokemon_parser.engine.watchlist import WatchlistTracker
from pokemon_parser.filters.engine import match
from pokemon_parser.filters.models import FilterRule
from pokemon_parser.models import ActionTarget, AddToCartTarget, ParsedItem, WatchlistCheckResult, WatchlistProduct
from pokemon_parser.parsers.mediamarkt import MediaMarktParser, MediaMarktParserDeny
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.utils.text import normalize_text


PRODUCT_TITLE = "POKEMON (UE) ME02.5 Ascended Heroes ETB Trading cards"
PRODUCT_URL = "https://www.mediamarkt.nl/nl/product/_pokemon-ue-me025-ascended-heroes-etb-trading-cards-1895844.html"
SYNTHETIC_FIXTURE_ID = "99000001"
SYNTHETIC_FIXTURE_TITLE = "Synthetic Trading Card Starter Box"
SYNTHETIC_FIXTURE_URL = "https://www.mediamarkt.nl/nl/product/_synthetic-trading-card-starter-box-99000001.html"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


class _Cfg:
    parser_concurrency = 1
    action_mode = "selenium"
    enable_notifications = False
    enable_success_alerts = True
    enable_error_alerts = False
    worker_telegram_trace_enabled = False
    bol_buy_now_url = "https://www.bol.com/nl/nl/checkout/?entryPoint=BUY_NOW"

    def parser_enabled_map(self) -> dict[str, bool]:
        return {"mediamarkt": True, "dreamland": True, "bol": True, "pocketgames": True}

    def is_parser_enabled(self, site: str) -> bool:
        return self.parser_enabled_map().get(site, False)

    def enabled_parser_sites(self):
        return tuple(site for site, enabled in self.parser_enabled_map().items() if enabled)

    def watchlist_enabled(self) -> bool:
        return True

    def watchlist_interval_seconds(self, site: str) -> float:
        return 3.0

    def watchlist_max_concurrency(self, site: str) -> int:
        return 1

    def watchlist_request_timeout_seconds(self, site: str | None = None) -> float:
        return 3.0

    def watchlist_jitter_seconds(self, site: str | None = None) -> float:
        return 0.0

    def watchlist_backoff_on_429(self) -> bool:
        return True

    def watchlist_backoff_multiplier(self) -> float:
        return 2.0

    def watchlist_max_backoff_seconds(self) -> float:
        return 60.0

    def watchlist_pause_site_on_error(self) -> bool:
        return False

    def site_request_timeout_seconds(self, site: str) -> float:
        return 3.0

    def site_max_pages(self, site: str) -> int | None:
        return 1 if site == "mediamarkt" else 3

    def site_page_delay_seconds(self, site: str) -> float:
        return 0.0

    def max_retries(self) -> int:
        return 0

    def retry_delay_seconds(self) -> float:
        return 0.0

    def mediamarkt_graphql_backoff_seconds(self) -> float:
        return 5.0

    def mediamarkt_graphql_backoff_multiplier(self) -> float:
        return 2.0

    def mediamarkt_graphql_max_backoff_seconds(self) -> float:
        return 30.0


class _Notifier:
    def is_enabled(self) -> bool:
        return False

    async def send(self, session, text: str, metadata=None) -> None:
        raise AssertionError("notifications are disabled")


class _FakeParser:
    site = "mediamarkt"

    def __init__(self, items, metrics=None):
        self.items = items
        self.last_scan_metrics = metrics or {}

    async def fetch(self, session, cfg):
        return list(self.items)


def _storage():
    conn = sqlite3.connect(":memory:")
    storage = SqliteStorage(conn)
    storage.init_schema()
    return conn, storage


def _filter(enabled=True):
    return FilterRule(
        id=4,
        name=PRODUCT_TITLE,
        sites=("mediamarkt",),
        include_groups=(("pokemon", "ascended", "heroes", "etb", "trading", "cards"),),
        min_price=75.0,
        max_price=85.0,
        enabled=enabled,
    )


def _synthetic_fixture_filter():
    return FilterRule(
        id=9901,
        name=SYNTHETIC_FIXTURE_TITLE,
        sites=("mediamarkt",),
        include_groups=(("synthetic", "trading", "card", "starter", "box"),),
        min_price=10.0,
        max_price=30.0,
        enabled=True,
    )


def _item(*, available=False, extra=None, target=None):
    if target is None and available:
        target = ActionTarget(
            site="mediamarkt",
            external_id="1895844",
            title=PRODUCT_TITLE,
            product_url=PRODUCT_URL,
            add_to_cart=AddToCartTarget(type="ui_button", product_id="1895844", product_url=PRODUCT_URL),
        )
    return ParsedItem(
        site="mediamarkt",
        external_id="1895844",
        title=PRODUCT_TITLE,
        title_norm=normalize_text(PRODUCT_TITLE),
        url=PRODUCT_URL,
        price_value=79.99,
        availability_text="available" if available else "unavailable",
        is_available=available,
        seller="mediamarkt",
        extra=extra or {"article_number": "1895844", "availability_status": "delivery_available" if available else "out_of_stock"},
        target=target,
    )


def test_filter_4_matches_ascended_heroes_price_range():
    assert match(_item(), _filter())


def test_mediamarkt_normalizes_article_number_and_url_to_same_product_key():
    parser = MediaMarktParser()
    assert parser._normalize_external_id("1895844", PRODUCT_URL) == "1895844"
    assert parser._normalize_external_id(PRODUCT_URL, PRODUCT_URL) == "1895844"


def test_mediamarkt_later_pdp_fixture_is_notify_only_not_purchasable():
    parser = MediaMarktParser()
    html = (FIXTURES_DIR / "mediamarkt_pdp_synthetic_unavailable.html").read_text(encoding="utf-8")
    item = parser.parse_pdp_html(html, SYNTHETIC_FIXTURE_URL)
    assert item is not None
    assert item.title == SYNTHETIC_FIXTURE_TITLE
    assert item.external_id == SYNTHETIC_FIXTURE_ID
    assert item.price_value == 19.99
    assert item.is_available is False
    assert item.target is None
    assert item.extra["availability_status"] == "notify_only"
    assert item.extra["raw_delivery_status"] == "NOT_AVAILABLE"
    assert item.extra["store_stock_unknown"] is True


def test_mediamarkt_pdp_available_fixture_is_high_confidence_actionable():
    parser = MediaMarktParser()
    html = (FIXTURES_DIR / "mediamarkt_pdp_available.html").read_text(encoding="utf-8")
    item = parser.parse_pdp_html(html, SYNTHETIC_FIXTURE_URL)
    assert item is not None
    assert item.external_id == SYNTHETIC_FIXTURE_ID
    assert item.price_value == 19.99
    assert item.extra["availability_status"] in {"add_to_cart_available", "delivery_available"}
    assert item.is_available is True
    assert item.target is not None
    assert item.extra["status_confidence_score"] >= 0.95
    diagnostic = item.extra["pdp_diagnostic"]
    assert diagnostic["article_number_found"] is True
    assert diagnostic["price_found"] is True
    assert diagnostic["delivery_available_marker"] is True
    assert diagnostic["online_status_available_marker"] is True
    assert diagnostic["add_to_cart_button_found"] is True
    assert diagnostic["add_to_cart_button_disabled"] is False
    assert diagnostic["action_target_exists"] is True


def test_mediamarkt_pdp_soon_fixture_is_notify_only_without_action_target():
    parser = MediaMarktParser()
    html = (FIXTURES_DIR / "mediamarkt_pdp_soon_available.html").read_text(encoding="utf-8")
    item = parser.parse_pdp_html(html, SYNTHETIC_FIXTURE_URL)
    assert item is not None
    assert item.extra["availability_status"] in {"soon_available", "notify_only"}
    assert item.is_available is False
    assert item.target is None
    assert 0.8 <= item.extra["status_confidence_score"] <= 0.9
    diagnostic = item.extra["pdp_diagnostic"]
    assert diagnostic["delivery_not_available_marker"] is True
    assert diagnostic["alert_button_found"] is True
    assert diagnostic["soon_available_text_found"] is True
    assert diagnostic["pickup_selector_found"] is True
    assert diagnostic["add_to_cart_button_found"] is False
    assert diagnostic["action_target_exists"] is False


def test_mediamarkt_pickup_selector_alone_does_not_make_pdp_purchasable():
    parser = MediaMarktParser()
    html = """
    <h1>POKEMON (UE) ME02.5 Ascended Heroes ETB Trading cards</h1>
    <div data-test="pdp-article-number">Art.-Nr. 1895844</div>
    <div data-test="mms-product-price">€ 79,99</div>
    <section data-test="mms-cofr-pickup_NO_STORE_SELECTED">
      Bekijk de winkelvoorraad voor ophalen
      <button type="button">Selecteer winkel</button>
    </section>
    """
    item = parser.parse_pdp_html(html, PRODUCT_URL)
    assert item is not None
    assert item.is_available is False
    assert item.target is None
    assert item.extra["store_stock_unknown"] is True
    assert item.extra["availability_status"] == "parse_unknown"


def test_mediamarkt_graphql_429_html_fallback_is_low_confidence_unknown():
    parser = MediaMarktParser()
    items = parser._build_page_items_from_products(
        [
            {
                "product_id": PRODUCT_URL,
                "title": PRODUCT_TITLE,
                "url": PRODUCT_URL,
                "price": 79.99,
                "in_stock": False,
                "raw": {"fallback": True},
            }
        ],
        page=1,
        global_seen=set(),
        source="html_category_fallback",
        soft_graphql_deny=True,
    )
    assert len(items) == 1
    item = items[0]
    assert item.external_id == "1895844"
    assert item.is_available is False
    assert item.target is None
    assert item.extra["availability_status"] == "rate_limited_unknown"
    assert item.extra["availability_confidence"] == "low"


def test_mediamarkt_high_confidence_available_creates_action_target():
    parser = MediaMarktParser()
    item = parser._build_page_items_from_products(
        [
            {
                "product_id": "1895844",
                "title": PRODUCT_TITLE,
                "url": PRODUCT_URL,
                "price": 79.99,
                "in_stock": True,
                "raw": {"inStock": True},
            }
        ],
        page=1,
        global_seen=set(),
        source="graphql",
    )[0]
    assert item.is_available is True
    assert item.extra["availability_status"] == "delivery_available"
    assert item.target is not None


def test_storage_watchlist_upsert_updates_duplicate_product():
    conn, storage = _storage()
    try:
        storage.upsert_watchlist_from_item(_item(), [_filter()], source="auto_filter_match")
        storage.upsert_watchlist_from_item(_item(available=True), [_filter()], source="auto_filter_match")
        rows = storage.list_watchlist()
        assert len(rows) == 1
        assert rows[0]["product_key"] == "1895844"
        assert rows[0]["current_inventory_status"] == "delivery_available"
    finally:
        conn.close()


def test_discovery_matched_product_auto_adds_to_watchlist_and_logs_skip_reason():
    conn, storage = _storage()
    try:
        storage.replace_filters([_filter()])
        dispatcher = SeleniumDispatcher(queue.Queue())
        pipeline = Pipeline(_Cfg(), storage, _Notifier(), dispatcher, antiban=None)
        asyncio.run(pipeline.run_site(object(), _FakeParser([_item(available=False)])))
        rows = storage.list_watchlist()
        assert len(rows) == 1
        assert rows[0]["source"] == "auto_filter_match"
        action_log = conn.execute("SELECT status, details FROM action_log ORDER BY id DESC LIMIT 1").fetchone()
        assert action_log[0] == "skip_action_unavailable"
        assert json.loads(action_log[1])["reason"] == "mediamarkt_unavailable"
    finally:
        conn.close()


def test_mediamarkt_soft_graphql_429_successful_fallback_skips_global_cooldown():
    conn, storage = _storage()
    try:
        storage.replace_filters([_filter()])
        antiban = AntiBanManager(
            {
                "mediamarkt": SitePolicy(
                    site="mediamarkt",
                    parser=LayerPolicy(
                        min_interval_seconds=0.0,
                        open_after_denies=2,
                        backoff=BackoffPolicy(base_seconds=5.0, max_seconds=30.0),
                    ),
                    worker=LayerPolicy(
                        min_interval_seconds=0.0,
                        open_after_denies=2,
                        backoff=BackoffPolicy(base_seconds=5.0, max_seconds=30.0),
                    ),
                )
            }
        )
        pipeline = Pipeline(_Cfg(), storage, _Notifier(), SeleniumDispatcher(queue.Queue()), antiban=antiban)
        item = _item(
            available=False,
            extra={
                "article_number": "1895844",
                "availability_status": "rate_limited_unknown",
                "rate_limited": True,
                "availability_confidence": "low",
            },
        )
        result = asyncio.run(
            pipeline.run_site(
                object(),
                _FakeParser(
                    [item],
                    metrics={
                        "scan_status": "recovered_via_fallback",
                        "failure_severity": "endpoint_quota_exceeded",
                        "source_mix": {"html_category_fallback": 1},
                        "items_fetched": 1,
                        "graphql_soft_denies": 1,
                        "html_fallback_pages": 1,
                        "html_fallback_pages_with_products": 1,
                        "global_cooldown_applied": False,
                        "isolated_backoff_applied": True,
                        "discovery_routing_mode": "fallback_only",
                        "graphql_endpoint_status": "circuit_open",
                        "events": [
                            {
                                "event": "graphql_circuit_open",
                                "page": 1,
                                "discovery_routing_mode": "fallback_only",
                            }
                        ],
                    },
                ),
            )
        )
        state = antiban.state["mediamarkt"].parser
        assert result.status == "recovered_via_fallback"
        assert state.consecutive_denies == 0
        assert state.last_deny_kind is None
        assert state.cooldown_until == 0
    finally:
        conn.close()


def test_mediamarkt_graphql_quota_failed_fallback_stays_source_local(monkeypatch):
    conn, storage = _storage()
    try:
        antiban = AntiBanManager(
            {
                "mediamarkt": SitePolicy(
                    site="mediamarkt",
                    parser=LayerPolicy(
                        min_interval_seconds=0.0,
                        open_after_denies=1,
                        backoff=BackoffPolicy(base_seconds=5.0, max_seconds=30.0),
                    ),
                    worker=LayerPolicy(
                        min_interval_seconds=0.0,
                        open_after_denies=1,
                        backoff=BackoffPolicy(base_seconds=5.0, max_seconds=30.0),
                    ),
                )
            }
        )
        parser = MediaMarktParser()

        async def raise_quota(session, cfg, page):
            raise MediaMarktParserDeny(
                request_info=None,
                history=(),
                status=429,
                message="graphql_quota_soft",
                headers=None,
            )

        async def fail_html(session, cfg, url):
            raise RuntimeError("fallback down")

        monkeypatch.setattr(parser, "_fetch_graphql_page", raise_quota)
        monkeypatch.setattr(parser, "_fetch_html", fail_html)

        pipeline = Pipeline(_Cfg(), storage, _Notifier(), SeleniumDispatcher(queue.Queue()), antiban=antiban)
        result = asyncio.run(pipeline.run_site(object(), parser))
        state = antiban.state["mediamarkt"].parser

        assert result.status == "partial_success"
        assert state.consecutive_denies == 0
        assert state.last_deny_kind is None
        assert state.cooldown_until == 0
        assert parser.last_scan_metrics["isolated_backoff_applied"] is True
        assert parser.last_scan_metrics["global_cooldown_applied"] is False
        assert parser.last_scan_metrics["failure_severity"] == "endpoint_degraded_no_authoritative_data"
    finally:
        conn.close()


def test_mediamarkt_graphql_circuit_routes_to_fallback_during_backoff(monkeypatch):
    parser = MediaMarktParser()
    graphql_calls = {"count": 0}
    fallback_product = {
        "product_id": PRODUCT_URL,
        "title": PRODUCT_TITLE,
        "url": PRODUCT_URL,
        "price": 79.99,
        "in_stock": False,
        "raw": {"fallback": True},
    }

    async def raise_quota_once(session, cfg, page):
        graphql_calls["count"] += 1
        raise MediaMarktParserDeny(
            request_info=None,
            history=(),
            status=429,
            message="graphql_quota_soft",
            headers=None,
        )

    async def fetch_html(session, cfg, url):
        return "<html><a href=\"/nl/product/_pokemon-ue-me025-ascended-heroes-etb-trading-cards-1895844.html\">Pokemon</a></html>"

    monkeypatch.setattr(parser, "_fetch_graphql_page", raise_quota_once)
    monkeypatch.setattr(parser, "_fetch_html", fetch_html)
    monkeypatch.setattr(parser, "_extract_products_from_html", lambda html: [dict(fallback_product)])

    first_items = asyncio.run(parser.fetch(object(), _Cfg()))
    first_metrics = parser.last_scan_metrics
    second_items = asyncio.run(parser.fetch(object(), _Cfg()))
    second_metrics = parser.last_scan_metrics

    assert len(first_items) == 1
    assert len(second_items) == 1
    assert graphql_calls["count"] == 1
    assert first_metrics["scan_status"] == "recovered_via_fallback"
    assert first_metrics["graphql_circuit_open"] is True
    assert second_metrics["fallback_routing_only_pages"] == 1
    assert second_metrics["discovery_routing_mode"] == "fallback_only"


def test_disabled_filter_does_not_auto_enroll():
    conn, storage = _storage()
    try:
        storage.replace_filters([_filter(enabled=False)])
        dispatcher = SeleniumDispatcher(queue.Queue())
        pipeline = Pipeline(_Cfg(), storage, _Notifier(), dispatcher, antiban=None)
        asyncio.run(pipeline.run_site(object(), _FakeParser([_item(available=False)])))
        assert storage.list_watchlist() == []
    finally:
        conn.close()


def test_build_from_filters_populates_watchlist_across_channels(monkeypatch):
    conn, storage = _storage()
    try:
        storage.replace_filters([_filter()])
        tracker = WatchlistTracker(cfg=_Cfg(), storage=storage, notifier=_Notifier())
        monkeypatch.setattr(
            "pokemon_parser.engine.watchlist.build_enabled_parser_registry",
            lambda cfg: {"mediamarkt": _FakeParser([_item(available=False)])},
        )
        result = asyncio.run(tracker.build_from_filters(object()))
        assert result["added_or_updated"] == 1
        assert storage.list_watchlist()[0]["product_key"] == "1895844"
    finally:
        conn.close()


def test_mediamarkt_watchlist_debug_artifacts_are_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MEDIAMARKT_WATCHLIST_DEBUG_ARTIFACTS", raising=False)
    conn, storage = _storage()
    try:
        cfg = _Cfg()
        cfg.base_dir = tmp_path
        tracker = WatchlistTracker(cfg=cfg, storage=storage, notifier=_Notifier())

        artifact_dir = tracker._save_mediamarkt_watchlist_artifacts(
            watch_item={"id": 1, "product_key": "1895844", "article_number": "1895844", "url": PRODUCT_URL},
            reason="html_rate_limited",
            html="<html>soft deny</html>",
            http_status=429,
        )

        assert artifact_dir is None
        assert not (tmp_path / "debug_artifacts" / "mediamarkt_watchlist").exists()
    finally:
        conn.close()


def test_mediamarkt_watchlist_debug_artifacts_honor_enabled_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIAMARKT_WATCHLIST_DEBUG_ARTIFACTS", "1")
    monkeypatch.setenv("MEDIAMARKT_WATCHLIST_DEBUG_ARTIFACT_LIMIT", "1")
    conn, storage = _storage()
    try:
        cfg = _Cfg()
        cfg.base_dir = tmp_path
        tracker = WatchlistTracker(cfg=cfg, storage=storage, notifier=_Notifier())
        watch_item = {"id": 1, "product_key": "1895844", "article_number": "1895844", "url": PRODUCT_URL}

        first_artifact_dir = tracker._save_mediamarkt_watchlist_artifacts(
            watch_item=watch_item,
            reason="html_rate_limited",
            html="<html>first soft deny</html>",
            http_status=429,
        )
        second_artifact_dir = tracker._save_mediamarkt_watchlist_artifacts(
            watch_item=watch_item,
            reason="html_rate_limited",
            html="<html>second soft deny</html>",
            http_status=429,
        )

        assert first_artifact_dir is not None
        assert (Path(first_artifact_dir) / "response.html").exists()
        assert second_artifact_dir is None
        root = tmp_path / "debug_artifacts" / "mediamarkt_watchlist"
        assert len(list(root.iterdir())) == 1
    finally:
        conn.close()


def test_watchlist_dreamland_404_remains_not_found_currently(monkeypatch):
    conn, storage = _storage()
    try:
        row = storage.upsert_watchlist_entry(
            WatchlistProduct(
                site="dreamland",
                product_key="abc",
                title="Pokemon Test",
                url="https://www.dreamland.nl/producten/abc",
                source="manual",
            )
        )
        tracker = WatchlistTracker(cfg=_Cfg(), storage=storage, notifier=_Notifier())

        async def raise_404(session, cfg, url):
            raise aiohttp.ClientResponseError(request_info=None, history=(), status=404, message="not found")

        monkeypatch.setattr(tracker.parsers["dreamland"], "fetch_product", raise_404)
        result = asyncio.run(tracker.scan_once(object(), item_id=row["id"]))
        assert result["checked"] == 1
        updated = storage.get_watchlist_item(row["id"])
        assert updated["current_inventory_status"] == "not_found_currently"
        assert updated["enabled"] is True
    finally:
        conn.close()


def test_watchlist_available_transition_queues_selenium_when_safe():
    conn, storage = _storage()
    try:
        storage.replace_filters([_filter()])
        dispatcher = SeleniumDispatcher(queue.Queue())
        tracker = WatchlistTracker(cfg=_Cfg(), storage=storage, notifier=_Notifier(), selenium_dispatcher=dispatcher)
        watch_item = storage.upsert_watchlist_from_item(_item(available=False), [_filter()])
        result = WatchlistCheckResult(
            site="mediamarkt",
            product_key="1895844",
            title=PRODUCT_TITLE,
            url=PRODUCT_URL,
            current_inventory_status="delivery_available",
            status_confidence_score=1.0,
            is_available=True,
            item=_item(available=True),
            action_target=_item(available=True).target,
        )
        _notification, selenium_queued, _skip = asyncio.run(tracker._maybe_trigger_actions(object(), watch_item, result))
        assert selenium_queued is True
        assert dispatcher.counts()["pending"] == 1
    finally:
        conn.close()


def test_watchlist_restock_transition_from_soon_to_available_is_actionable(monkeypatch):
    conn, storage = _storage()
    try:
        fixture_filter = _synthetic_fixture_filter()
        storage.replace_filters([fixture_filter])
        parser = MediaMarktParser()
        soon_html = (FIXTURES_DIR / "mediamarkt_pdp_soon_available.html").read_text(encoding="utf-8")
        available_html = (FIXTURES_DIR / "mediamarkt_pdp_available.html").read_text(encoding="utf-8")
        watch_item = storage.upsert_watchlist_from_item(
            parser.parse_pdp_html(soon_html, SYNTHETIC_FIXTURE_URL),
            [fixture_filter],
        )
        dispatcher = SeleniumDispatcher(queue.Queue())
        tracker = WatchlistTracker(cfg=_Cfg(), storage=storage, notifier=_Notifier(), selenium_dispatcher=dispatcher)

        async def fake_fetch_html(session, cfg, url):
            return available_html

        monkeypatch.setattr(tracker.parsers["mediamarkt"], "_fetch_html", fake_fetch_html)
        result = asyncio.run(tracker.scan_once(object(), item_id=watch_item["id"]))
        decision = result["results"][0]["result"]
        assert decision["previous_inventory_status"] in {"soon_available", "notify_only"}
        assert decision["new_inventory_status"] in {"add_to_cart_available", "delivery_available"}
        assert decision["status_changed"] is True
        assert decision["action_target_exists"] is True
        assert decision["selenium_queued"] is True
        assert decision["skip_reason"] == "ready_for_action"
        assert dispatcher.counts()["pending"] == 1
        site_diagnostics = storage.watchlist_site_diagnostics()["mediamarkt"]
        assert site_diagnostics["last_status"] in {"add_to_cart_available", "delivery_available"}
        assert site_diagnostics["last_action_target_exists"] is True
        assert site_diagnostics["buyable_marker_found"] is True
    finally:
        conn.close()


def test_watchlist_soon_state_does_not_queue_selenium(monkeypatch):
    conn, storage = _storage()
    try:
        storage.replace_filters([_filter()])
        parser = MediaMarktParser()
        soon_html = (FIXTURES_DIR / "mediamarkt_pdp_soon_available.html").read_text(encoding="utf-8")
        watch_item = storage.upsert_watchlist_from_item(_item(available=False), [_filter()])
        dispatcher = SeleniumDispatcher(queue.Queue())
        tracker = WatchlistTracker(cfg=_Cfg(), storage=storage, notifier=_Notifier(), selenium_dispatcher=dispatcher)

        async def fake_fetch_html(session, cfg, url):
            return soon_html

        monkeypatch.setattr(tracker.parsers["mediamarkt"], "_fetch_html", fake_fetch_html)
        result = asyncio.run(tracker.scan_once(object(), item_id=watch_item["id"]))
        decision = result["results"][0]["result"]
        assert decision["new_inventory_status"] in {"soon_available", "notify_only"}
        assert decision["action_target_exists"] is False
        assert decision["selenium_queued"] is False
        assert decision["skip_reason"] == "watchlist_not_high_confidence_available"
        assert dispatcher.counts()["total"] == 0
        site_diagnostics = storage.watchlist_site_diagnostics()["mediamarkt"]
        assert site_diagnostics["last_action_target_exists"] is False
        assert site_diagnostics["alert_notify_marker_found"] is True
    finally:
        conn.close()


def test_watchlist_unavailable_does_not_trigger_selenium():
    conn, storage = _storage()
    try:
        dispatcher = SeleniumDispatcher(queue.Queue())
        tracker = WatchlistTracker(cfg=_Cfg(), storage=storage, notifier=_Notifier(), selenium_dispatcher=dispatcher)
        watch_item = storage.upsert_watchlist_from_item(_item(available=False), [_filter()])
        result = WatchlistCheckResult(
            site="mediamarkt",
            product_key="1895844",
            title=PRODUCT_TITLE,
            url=PRODUCT_URL,
            current_inventory_status="out_of_stock",
            status_confidence_score=0.8,
            is_available=False,
            item=_item(available=False),
            action_target=None,
        )
        _notification, selenium_queued, skip = asyncio.run(tracker._maybe_trigger_actions(object(), watch_item, result))
        assert selenium_queued is False
        assert skip == "watchlist_available_low_confidence_or_missing_target"
        assert dispatcher.counts()["total"] == 0
    finally:
        conn.close()


def test_watchlist_api_summary_returns_counts(monkeypatch):
    class Manager:
        def summary(self):
            return {"ok": True, "total": 1, "enabled": 1, "available": 0, "sites": {"mediamarkt": {"total": 1}}}

    monkeypatch.setattr(watchlist_routes, "get_watchlist_manager", lambda: Manager())
    payload = watchlist_routes.watchlist_summary()
    assert payload["ok"] is True
    assert payload["sites"]["mediamarkt"]["total"] == 1
