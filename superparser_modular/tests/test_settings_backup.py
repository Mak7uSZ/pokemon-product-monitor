import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pokemon_parser.api.services.config_manager import ConfigManager
from pokemon_parser.api.services.settings_backup_manager import SettingsBackupManager
from pokemon_parser.api.services.shared import storage_context
from pokemon_parser.filters.models import FilterRule
from pokemon_parser.models import WatchlistProduct


ENV_KEYS = [
    "DB_FILE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "PROXY_ENABLED",
    "PROXY_TYPE",
    "PROXY_HOST",
    "PROXY_PORT",
    "PROXY_LOGIN",
    "PROXY_PASSWORD",
    "CHECKOUT_EMAIL",
    "CHECKOUT_CARD_NUMBER",
    "CHECKOUT_CARD_CVV",
    "CHROME_BINARY",
    "CHROME_USER_DATA_DIR",
    "CHROME_PROFILE_DIR",
    "ENABLE_MEDIAMARKT",
    "ENABLE_DREAMLAND",
    "ENABLE_BOL",
    "ENABLE_POCKETGAMES",
    "ENABLE_TIMER",
    "TIMER_INTERVAL",
    "TIMER_UNIT",
]
TEST_CARD_NUMBER = "4111" * 4


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_env(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "DB_FILE=monitor.db",
                "TELEGRAM_BOT_TOKEN=fixture-token",
                "TELEGRAM_CHAT_ID=secret-chat",
                "PROXY_ENABLED=true",
                "PROXY_TYPE=http",
                "PROXY_HOST=old.proxy.local",
                "PROXY_PORT=8080",
                "PROXY_LOGIN=secret-login",
                "PROXY_PASSWORD=secret-password",
                "CHECKOUT_EMAIL=buyer@example.test",
                f"CHECKOUT_CARD_NUMBER={TEST_CARD_NUMBER}",
                "CHECKOUT_CARD_CVV=123",
                "CHROME_BINARY=C:/private/chrome.exe",
                "CHROME_USER_DATA_DIR=C:/private/chrome-profile",
                "CHROME_PROFILE_DIR=Default",
                "ENABLE_MEDIAMARKT=true",
                "ENABLE_DREAMLAND=true",
                "ENABLE_BOL=true",
                "ENABLE_POCKETGAMES=true",
                "ENABLE_TIMER=false",
                "TIMER_INTERVAL=15",
                "TIMER_UNIT=minutes",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _manager(tmp_path):
    _write_env(tmp_path)
    return SettingsBackupManager(
        config_manager=ConfigManager(tmp_path),
        paths=SimpleNamespace(repo_root=tmp_path, app_root=tmp_path),
    )


def _seed_storage(manager):
    cfg = manager.config_manager.load_app_config()
    with storage_context(cfg) as storage:
        storage.replace_filters(
            [
                FilterRule(
                    id=1,
                    name="Pokemon ETB",
                    sites=("mediamarkt",),
                    include_groups=(("pokemon", "etb"),),
                    max_price=70.0,
                    enabled=True,
                )
            ]
        )
        storage.upsert_watchlist_entry(
            WatchlistProduct(
                site="mediamarkt",
                product_key="1895844",
                title="Pokemon ETB",
                url="https://example.test/products/1895844",
                pinned=False,
                source="manual",
            )
        )


def test_export_returns_valid_schema_and_omits_private_fields_by_default(tmp_path):
    manager = _manager(tmp_path)
    _seed_storage(manager)

    backup = manager.export_backup()
    serialized = json.dumps(backup)

    assert backup["schema_version"] == 1
    assert backup["app"]["name"] == "pokemon_parser"
    assert backup["filters"]
    assert backup["watchlist_items"]
    assert backup["network_preferences"] == {
        "enabled": True,
        "type": "http",
        "host": "old.proxy.local",
        "port": 8080,
    }
    assert "secret-token" not in serialized
    assert "secret-chat" not in serialized
    assert "secret-password" not in serialized
    assert "secret-login" not in serialized
    assert TEST_CARD_NUMBER not in serialized
    assert "C:/private/chrome-profile" not in serialized


def test_preview_detects_changes(tmp_path):
    manager = _manager(tmp_path)
    backup = manager.export_backup()
    backup["runtime_preferences"]["interval"] = 45
    backup["channels"]["bol"] = False

    preview = manager.preview_restore(backup)

    assert preview["valid"] is True
    assert preview["will_change"] is True
    assert "runtime_preferences" in preview["groups_changed"]
    assert "channels" in preview["groups_changed"]


def test_invalid_schema_is_rejected(tmp_path):
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="schema_version"):
        manager.preview_restore({"schema_version": 999})


def test_restore_does_not_overwrite_private_fields_with_placeholders(tmp_path):
    manager = _manager(tmp_path)
    backup = manager.export_backup()
    backup["network_preferences"] = {
        "enabled": True,
        "type": "socks5",
        "host": "new.proxy.local",
        "port": 1080,
        "login": "***",
        "password": "***",
    }
    backup["telegram_settings"] = {"bot_token": "***", "chat_id": "***"}

    manager.restore(backup)
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")

    assert "PROXY_HOST=new.proxy.local" in env_text
    assert "PROXY_PASSWORD=secret-password" in env_text
    assert "PROXY_LOGIN=secret-login" in env_text
    assert "TELEGRAM_BOT_TOKEN=fixture-token" in env_text
    assert "TELEGRAM_CHAT_ID=secret-chat" in env_text


def test_restore_upserts_filters_and_watchlist_items(tmp_path):
    manager = _manager(tmp_path)
    _seed_storage(manager)
    backup = manager.export_backup()
    backup["filters"] = [
        {
            "name": "Pokemon ETB",
            "sites": ["mediamarkt"],
            "keyword_groups": [["pokemon", "elite trainer box"]],
            "exclude_words": ["damaged"],
            "max_price": 55.0,
            "soft_price": True,
            "enabled": True,
        },
        {
            "name": "Pokemon Booster",
            "sites": ["bol"],
            "keyword_groups": [["pokemon", "booster"]],
            "enabled": True,
        },
    ]
    backup["watchlist_items"] = [
        {
            "site": "mediamarkt",
            "product_key": "1895844",
            "title": "Updated ETB",
            "url": "https://example.test/products/1895844",
            "pinned": True,
            "enabled": False,
            "source": "imported",
        },
        {
            "site": "bol",
            "product_key": "bol-123",
            "title": "New Booster",
            "url": "https://example.test/products/bol-123",
            "pinned": True,
            "enabled": True,
            "source": "imported",
        },
    ]

    result = manager.restore(backup)

    assert result["applied"]["filters"] == {"created": 1, "updated": 1, "unchanged": 0, "skipped": 0}
    assert result["applied"]["watchlist_items"] == {"created": 1, "updated": 1, "unchanged": 0, "skipped": 0}

    cfg = manager.config_manager.load_app_config()
    with storage_context(cfg) as storage:
        filters = {rule.name: rule for rule in storage.list_filters_all()}
        watchlist = {
            (item["site"], item["product_key"]): item
            for item in storage.list_watchlist(limit=2000)
        }

    assert filters["Pokemon ETB"].max_price == 55.0
    assert filters["Pokemon Booster"].sites == ("bol",)
    assert watchlist[("mediamarkt", "1895844")]["title"] == "Updated ETB"
    assert watchlist[("mediamarkt", "1895844")]["pinned"] is True
    assert watchlist[("mediamarkt", "1895844")]["enabled"] is False
    assert watchlist[("bol", "bol-123")]["title"] == "New Booster"


def test_local_backup_is_created_before_restore(tmp_path):
    manager = _manager(tmp_path)
    backup = manager.export_backup()
    backup["runtime_preferences"]["interval"] = 33

    result = manager.restore(backup)
    snapshot_path = result["pre_restore_snapshot"]["path"]
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))

    assert snapshot["runtime_preferences"]["interval"] == 15
    assert manager.config_manager.get_timer_settings()["interval"] == 33
