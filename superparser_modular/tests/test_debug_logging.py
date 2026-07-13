import asyncio
import json
import logging
import queue
import sqlite3
from types import SimpleNamespace

import pytest

from pokemon_parser.api.routes.logs import get_debug_logs
from pokemon_parser.api.services.logs_manager import LogsManager
from pokemon_parser.engine.pipeline import Pipeline
from pokemon_parser.engine.selenium_dispatcher import SeleniumDispatcher
from pokemon_parser.filters.models import FilterRule
from pokemon_parser.models import ActionTarget, AddToCartTarget, ParsedItem
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.utils.logging_setup import debug_log_paths, setup_debug_logging
from pokemon_parser.utils.text import normalize_text
from pokemon_parser.workers.selenium_worker import SeleniumWorker


@pytest.fixture(autouse=True)
def enable_private_debug_files_for_tests(monkeypatch):
    monkeypatch.setenv("POKEMON_DEBUG_LOGS", "1")


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

    async def send(self, session, text: str, *, metadata: dict | None = None) -> None:
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


def test_logging_setup_creates_debug_files(tmp_path):
    paths = setup_debug_logging(tmp_path, force=True)
    logging.getLogger("pokemon_parser.test").info("hello diagnostics")

    for path in paths.values():
        assert path.exists()


def test_selenium_startup_failure_is_logged_and_snapshotted(tmp_path, monkeypatch):
    setup_debug_logging(tmp_path, force=True)

    from selenium import webdriver

    def _raise_driver_failure(*args, **kwargs):
        raise RuntimeError("chrome failed immediately")

    monkeypatch.setattr(webdriver, "Chrome", _raise_driver_failure)

    profile_root = tmp_path / "chrome-profile"
    profile_root.mkdir()
    cfg = SimpleNamespace(
        base_dir=tmp_path,
        chrome_binary="",
        chrome_user_data_dir=str(profile_root),
        chrome_profile_dir="Default",
        proxy_enabled=False,
        proxy_host="",
        proxy_port=0,
        proxy_type="http",
    )
    worker = SeleniumWorker(cfg=cfg, job_queue=queue.Queue())

    with pytest.raises(RuntimeError, match="chrome failed immediately"):
        worker.init_driver()

    paths = debug_log_paths(tmp_path)
    assert "webdriver.Chrome failed" in paths["errors"].read_text(encoding="utf-8")
    assert "chrome failed immediately" in paths["selenium_worker"].read_text(encoding="utf-8")
    snapshots = list((tmp_path / "debug_logs").glob("selenium_startup_failure_*.txt"))
    assert snapshots
    assert "ChromeDriver mismatch" in snapshots[0].read_text(encoding="utf-8")


def test_debug_logs_redact_secrets(tmp_path):
    setup_debug_logging(tmp_path, force=True)

    logging.getLogger("pokemon_parser.test").error(
        "token=bot123456:ABCdef_SECRET password=hunter2 webhook=https://example.test/hook"
    )

    text = debug_log_paths(tmp_path)["errors"].read_text(encoding="utf-8")
    assert "bot123456:ABCdef_SECRET" not in text
    assert "hunter2" not in text
    assert "https://example.test/hook" not in text
    assert "<redacted>" in text


def test_scan_decision_log_includes_skip_reason_and_matched_filters(tmp_path):
    setup_debug_logging(tmp_path, force=True)
    conn, storage = _storage()
    try:
        storage.replace_filters([_rule()])
        dispatcher = SeleniumDispatcher(queue.Queue())
        pipeline = _pipeline(storage, dispatcher)

        asyncio.run(pipeline.run_site(object(), _FakeParser([_item(available=False)])))

        text = debug_log_paths(tmp_path)["scan_decisions"].read_text(encoding="utf-8")
        assert '"skip_reason":"mediamarkt_unavailable"' in text
        assert '"matched_filter_ids":[1]' in text
        assert '"matched_filter_names":["Pokemon booster"]' in text
    finally:
        conn.close()


def test_debug_log_api_returns_latest_lines(tmp_path, monkeypatch):
    setup_debug_logging(tmp_path, force=True)
    logger = logging.getLogger("pokemon_parser.api_test")
    logger.error("first api line")
    logger.error("second api line")
    logger.error("third api line")

    class _ConfigManager:
        def load_app_config(self):
            return SimpleNamespace(base_dir=tmp_path)

    monkeypatch.setattr(
        "pokemon_parser.api.routes.logs.get_logs_manager",
        lambda: LogsManager(config_manager=_ConfigManager()),
    )

    payload = get_debug_logs(lines=2)
    latest_errors = payload["files"]["errors"]["latest"]
    assert any("second api line" in line for line in latest_errors)
    assert any("third api line" in line for line in latest_errors)
    assert not any("first api line" in line for line in latest_errors)
