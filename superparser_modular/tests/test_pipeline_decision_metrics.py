import asyncio
import json
import queue
import sqlite3
from pathlib import Path

from pokemon_parser.engine.pipeline import Pipeline
from pokemon_parser.engine.selenium_dispatcher import SeleniumDispatcher
from pokemon_parser.engine.startup import ZERO_ENABLED_FILTERS_WARNING
from pokemon_parser.filters.models import FilterRule
from pokemon_parser.models import ActionTarget, AddToCartTarget, ParsedItem
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.utils.text import normalize_text


class _PipelineCfg:
    parser_concurrency = 1
    action_mode = "selenium"
    enable_notifications = False
    enable_success_alerts = True
    enable_error_alerts = False
    worker_telegram_trace_enabled = False

    def parser_enabled_map(self) -> dict[str, bool]:
        return {
            "mediamarkt": True,
            "dreamland": False,
            "bol": False,
            "pocketgames": False,
        }

    def is_parser_enabled(self, site: str) -> bool:
        return self.parser_enabled_map().get(site, False)


class _Notifier:
    def is_enabled(self) -> bool:
        return False

    async def send(self, session, text: str) -> None:
        raise AssertionError("telegram should be disabled in this test")


class _FakeParser:
    site = "mediamarkt"

    def __init__(self, items: list[ParsedItem]):
        self.items = items

    async def fetch(self, session, cfg) -> list[ParsedItem]:
        return list(self.items)


def _storage() -> tuple[sqlite3.Connection, SqliteStorage]:
    conn = sqlite3.connect(":memory:")
    storage = SqliteStorage(conn)
    storage.init_schema()
    return conn, storage


def _rule() -> FilterRule:
    return FilterRule(
        id=1,
        name="Pokemon booster",
        sites=("mediamarkt",),
        include_groups=(("pokemon", "booster"),),
        enabled=True,
    )


def _item(*, available: bool) -> ParsedItem:
    title = "Pokemon Booster Bundle"
    target = None
    if available:
        target = ActionTarget(
            site="mediamarkt",
            external_id="mm-1",
            title=title,
            product_url="https://example.test/pokemon-booster",
            add_to_cart=AddToCartTarget(
                type="direct_url",
                add_to_cart_url="https://example.test/cart/add/mm-1",
                product_url="https://example.test/pokemon-booster",
            ),
        )

    return ParsedItem(
        site="mediamarkt",
        external_id="mm-1",
        title=title,
        title_norm=normalize_text(title),
        url="https://example.test/pokemon-booster",
        price_value=12.99,
        availability_text="Op voorraad" if available else "Niet op voorraad",
        is_available=available,
        seller="mediamarkt",
        target=target,
    )


def _pipeline(storage: SqliteStorage, dispatcher: SeleniumDispatcher) -> Pipeline:
    return Pipeline(
        cfg=_PipelineCfg(),
        storage=storage,
        notifier=_Notifier(),
        selenium_dispatcher=dispatcher,
        antiban=None,
        runtime_state=None,
    )


def test_unavailable_to_available_restock_queues_selenium_job():
    conn, storage = _storage()
    try:
        storage.replace_filters([_rule()])
        job_queue: "queue.Queue" = queue.Queue()
        dispatcher = SeleniumDispatcher(job_queue)
        pipeline = _pipeline(storage, dispatcher)

        asyncio.run(pipeline.run_site(object(), _FakeParser([_item(available=False)])))
        assert dispatcher.counts()["total"] == 0

        asyncio.run(pipeline.run_site(object(), _FakeParser([_item(available=True)])))

        assert dispatcher.counts()["pending"] == 1
        assert job_queue.qsize() == 1

        restock = conn.execute(
            """
            SELECT matched_filter_ids_json
            FROM events
            WHERE event_type = 'restock'
            """
        ).fetchone()
        assert restock is not None
        assert json.loads(restock[0]) == [1]

        metrics = conn.execute(
            """
            SELECT details_json
            FROM runtime_logs
            WHERE message = 'filter/action decision metrics'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert metrics is not None
        payload = json.loads(metrics[0])
        assert payload["products_parsed"] == 1
        assert payload["enabled_filters"] == 1
        assert payload["queued_selenium_jobs"] == 1
    finally:
        conn.close()


def test_available_item_with_empty_filters_does_not_queue_and_logs_warning():
    conn, storage = _storage()
    try:
        job_queue: "queue.Queue" = queue.Queue()
        dispatcher = SeleniumDispatcher(job_queue)
        pipeline = _pipeline(storage, dispatcher)

        asyncio.run(pipeline.run_site(object(), _FakeParser([_item(available=True)])))

        assert dispatcher.counts()["total"] == 0
        assert job_queue.qsize() == 0

        warning = conn.execute(
            """
            SELECT message
            FROM runtime_logs
            WHERE message = ?
            """,
            (ZERO_ENABLED_FILTERS_WARNING,),
        ).fetchone()
        assert warning is not None

        metrics = conn.execute(
            """
            SELECT details_json
            FROM runtime_logs
            WHERE message = 'filter/action decision metrics'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert metrics is not None
        payload = json.loads(metrics[0])
        assert payload["filters_loaded"] == 0
        assert payload["enabled_filters"] == 0
        assert payload["queued_selenium_jobs"] == 0
    finally:
        conn.close()


def test_unchanged_available_item_does_not_requeue_after_restart(tmp_path: Path):
    database_path = tmp_path / "pipeline.db"
    first_conn = sqlite3.connect(str(database_path))
    first_storage = SqliteStorage(first_conn)
    first_storage.init_schema()
    first_storage.replace_filters([_rule()])
    first_queue: "queue.Queue" = queue.Queue()
    first_dispatcher = SeleniumDispatcher(first_queue)
    first_pipeline = _pipeline(first_storage, first_dispatcher)

    first_result = asyncio.run(first_pipeline.run_site(object(), _FakeParser([_item(available=True)])))
    assert first_result.events_found == 1
    assert first_dispatcher.counts()["pending"] == 1
    first_conn.close()

    second_conn = sqlite3.connect(str(database_path))
    try:
        second_storage = SqliteStorage(second_conn)
        second_storage.init_schema()
        second_queue: "queue.Queue" = queue.Queue()
        second_dispatcher = SeleniumDispatcher(second_queue)
        second_pipeline = _pipeline(second_storage, second_dispatcher)

        second_result = asyncio.run(second_pipeline.run_site(object(), _FakeParser([_item(available=True)])))

        assert second_result.events_found == 0
        assert second_dispatcher.counts()["total"] == 0
        metrics = second_conn.execute(
            """
            SELECT details_json FROM runtime_logs
            WHERE message = 'filter/action decision metrics'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        assert metrics is not None
        assert json.loads(metrics[0])["unchanged_action_skips"] == 1
    finally:
        second_conn.close()


def test_partial_scan_omission_does_not_mark_existing_product_missing():
    conn, storage = _storage()
    try:
        first = _item(available=False)
        second = ParsedItem(
            site="mediamarkt",
            external_id="mm-2",
            title="Pokemon Booster Two",
            title_norm="pokemon booster two",
            url="https://example.test/pokemon-booster-two",
            price_value=19.99,
            availability_text="Niet op voorraad",
            is_available=False,
            seller="mediamarkt",
        )
        storage.upsert_items([first, second])
        pipeline = _pipeline(storage, SeleniumDispatcher(queue.Queue()))

        asyncio.run(pipeline.run_site(object(), _FakeParser([first])))

        state = storage.product_state_map("mediamarkt", ["mm-2"])["mm-2"]
        assert state["active"] is True
        assert conn.execute(
            "SELECT missing_count FROM products WHERE site = 'mediamarkt' AND external_id = 'mm-2'"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_unavailable_bol_item_never_queues_selenium_action():
    conn, storage = _storage()
    try:
        storage.replace_filters(
            [
                FilterRule(
                    id=1,
                    name="Bol Pokemon booster",
                    sites=("bol",),
                    include_groups=(("pokemon", "booster"),),
                    enabled=True,
                )
            ]
        )
        title = "Pokemon Booster Bundle"
        item = ParsedItem(
            site="bol",
            external_id="9300000123456",
            title=title,
            title_norm=normalize_text(title),
            url="https://www.bol.com/nl/nl/p/pokemon-booster/9300000123456/",
            price_value=12.99,
            availability_text="Niet leverbaar",
            is_available=False,
            seller="bol",
            target=ActionTarget(
                site="bol",
                external_id="9300000123456",
                title=title,
                product_url="https://www.bol.com/nl/nl/p/pokemon-booster/9300000123456/",
                add_to_cart=AddToCartTarget(
                    type="direct_url",
                    add_to_cart_url="https://www.bol.com/nl/order/basket/add/9300000123456",
                    product_url="https://www.bol.com/nl/nl/p/pokemon-booster/9300000123456/",
                ),
            ),
        )
        parser = _FakeParser([item])
        parser.site = "bol"
        job_queue: "queue.Queue" = queue.Queue()
        dispatcher = SeleniumDispatcher(job_queue)
        pipeline = _pipeline(storage, dispatcher)

        asyncio.run(pipeline.run_site(object(), parser))

        assert dispatcher.counts()["total"] == 0
        assert job_queue.qsize() == 0
        action_log = conn.execute(
            """
            SELECT status, details
            FROM action_log
            WHERE site = 'bol' AND external_id = '9300000123456'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert action_log is not None
        assert action_log[0] == "skip_action_unavailable"
        assert json.loads(action_log[1])["reason"] == "bol_unavailable"
    finally:
        conn.close()
