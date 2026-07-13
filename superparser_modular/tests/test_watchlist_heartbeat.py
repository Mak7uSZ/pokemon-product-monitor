import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pokemon_parser.api.services.runtime_manager import RuntimeManager
from pokemon_parser.engine.heartbeat import build_heartbeat_text
from pokemon_parser.engine.runtime_state import RuntimeStateStore, WatchlistRuntimeState
from pokemon_parser.engine.watchlist import WatchlistTracker
from pokemon_parser.models import WatchlistCheckResult, WatchlistProduct
from pokemon_parser.parsers import SITE_LABELS
from pokemon_parser.storage.sqlite import SqliteStorage


class _Cfg:
    parser_concurrency = 4
    action_mode = "selenium"
    enable_notifications = False
    bol_buy_now_url = "https://www.bol.com/nl/nl/checkout/?entryPoint=BUY_NOW"

    def __init__(self, *, watchlist_enabled=True, base_dir: Path | None = None):
        self._watchlist_enabled = watchlist_enabled
        self.base_dir = base_dir or Path.cwd()

    def parser_enabled_map(self) -> dict[str, bool]:
        return {"mediamarkt": True, "dreamland": True, "bol": True, "pocketgames": True}

    def enabled_parser_sites(self):
        return tuple(site for site, enabled in self.parser_enabled_map().items() if enabled)

    def watchlist_enabled(self) -> bool:
        return self._watchlist_enabled

    def watchlist_site_enabled(self, site: str) -> bool:
        return True

    def watchlist_interval_seconds(self, site: str) -> float:
        return 8.0 if site in {"mediamarkt", "pocketgames"} else 12.0

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
        return 300.0

    def resolved_db_path(self) -> Path:
        return self.base_dir / "test.db"


def _storage():
    conn = sqlite3.connect(":memory:")
    storage = SqliteStorage(conn)
    storage.init_schema()
    return conn, storage


def _watchlist_item(storage: SqliteStorage):
    return storage.upsert_watchlist_entry(
        WatchlistProduct(
            site="mediamarkt",
            product_key="1895844",
            article_number="1895844",
            title="POKEMON (UE) ME02.5 Ascended Heroes ETB Trading cards",
            url="https://www.mediamarkt.nl/nl/product/_pokemon-ue-me025-ascended-heroes-etb-trading-cards-1895844.html",
            current_inventory_status="out_of_stock",
            enabled=True,
        )
    )


def test_runtime_overview_includes_watchlist(tmp_path):
    cfg = _Cfg(base_dir=tmp_path)

    class _ConfigManager:
        def load_app_config(self):
            return cfg

        def get_timer_settings(self):
            return {"enabled": False, "interval": 15, "unit": "minutes", "interval_seconds": 900}

        def get_scan_settings_effective(self):
            return {"watchlist": {"enabled": True}}

    manager = RuntimeManager(
        paths=SimpleNamespace(repo_root=tmp_path, app_root=tmp_path),
        config_manager=_ConfigManager(),
    )
    try:
        overview = manager.build_overview()
        assert "watchlist" in overview
        assert overview["watchlist"]["enabled"] is True
        assert overview["watchlist"]["intervals"]["mediamarkt"] == 8.0
        assert overview["watchlist"]["total_watchlist_items"] == 0
        assert overview["watchlist"]["actively_monitored_watchlist_items"] == 0
        assert overview["site_states"]["mediamarkt"]["graphql_circuit_open"] is False
        assert overview["site_states"]["mediamarkt"]["discovery_routing_mode"] == "normal"
        assert overview["endpoint_statuses"]["mediamarkt"]["graphql_endpoint_status"] == "active"
    finally:
        manager.close()


def test_heartbeat_text_includes_watchlist_status():
    cfg = _Cfg()
    discovery_state = RuntimeStateStore(
        site_labels=SITE_LABELS,
        enabled_map=cfg.parser_enabled_map(),
        action_mode=cfg.action_mode,
        scan_concurrency=cfg.parser_concurrency,
    )
    watchlist_state = WatchlistRuntimeState(cfg=cfg, site_labels=SITE_LABELS)
    watchlist_state.mark_loop_started()
    watchlist_state.mark_cycle_started()
    watchlist_state.mark_cycle_finished(
        duration_seconds=0.25,
        checked_count=6,
        changed_count=1,
        error_count=0,
        skipped_count=0,
        next_cycle_in_seconds=8,
    )
    overview = discovery_state.snapshot_overview(queue_size=0, selenium=None)
    overview["watchlist"] = watchlist_state.snapshot()

    text = build_heartbeat_text(runtime_overview=overview, antiban=None)
    assert "Priority Watchlist" in text
    assert "enabled=true" in text
    assert "running=true" in text
    assert "last_checked=6" in text
    assert "changed=1" in text


def test_watchlist_tracker_cycle_updates_runtime_state(monkeypatch):
    conn, storage = _storage()
    try:
        _watchlist_item(storage)
        cfg = _Cfg()
        state = WatchlistRuntimeState(cfg=cfg, site_labels=SITE_LABELS)
        tracker = WatchlistTracker(cfg=cfg, storage=storage, runtime_state=state)

        async def _check(session, watch_item):
            return WatchlistCheckResult(
                site="mediamarkt",
                product_key=watch_item["product_key"],
                title=watch_item["title"],
                url=watch_item["url"],
                current_inventory_status="out_of_stock",
                status_confidence_score=0.8,
                source_endpoint="test",
                http_status=200,
                duration_seconds=0.01,
            )

        monkeypatch.setattr(tracker, "_check_item", _check)
        result = asyncio.run(tracker.scan_once(object()))
        snapshot = state.snapshot(storage=storage)

        assert result["checked"] == 1
        assert snapshot["last_cycle_checked_count"] == 1
        assert snapshot["last_cycle_error_count"] == 0
        assert snapshot["total_watchlist_items"] == 1
        assert snapshot["total_enabled_watchlist_items"] == 1
        assert snapshot["actively_monitored_watchlist_items"] == 1
    finally:
        conn.close()


def test_disabled_watchlist_reports_not_running():
    conn, storage = _storage()
    try:
        cfg = _Cfg(watchlist_enabled=False)
        state = WatchlistRuntimeState(cfg=cfg, site_labels=SITE_LABELS)
        tracker = WatchlistTracker(cfg=cfg, storage=storage, runtime_state=state)

        result = asyncio.run(tracker.scan_once(object()))
        snapshot = state.snapshot(storage=storage)

        assert result["ok"] is False
        assert snapshot["enabled"] is False
        assert snapshot["running"] is False
    finally:
        conn.close()


def test_watchlist_error_is_reported_in_state_and_heartbeat(monkeypatch):
    conn, storage = _storage()
    try:
        _watchlist_item(storage)
        cfg = _Cfg()
        state = WatchlistRuntimeState(cfg=cfg, site_labels=SITE_LABELS)
        tracker = WatchlistTracker(cfg=cfg, storage=storage, runtime_state=state)

        async def _raise(session, watch_item):
            raise RuntimeError("boom")

        monkeypatch.setattr(tracker, "_check_item", _raise)
        result = asyncio.run(tracker.scan_once(object()))
        snapshot = state.snapshot(storage=storage)
        text = build_heartbeat_text(runtime_overview={"watchlist": snapshot}, antiban=None)

        assert result["checked"] == 1
        assert snapshot["last_cycle_error_count"] == 1
        assert "errors=1" in text
    finally:
        conn.close()
