import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from pokemon_parser.api.services.config_manager import ConfigManager
from pokemon_parser.config import _int_or_none, load_config


def _scan_settings_payload():
    discovery = {
        "enabled": True,
        "scan_delay_seconds": 10,
        "cooldown_seconds": 30,
        "request_timeout_seconds": 20,
        "max_pages": 5,
        "page_delay_seconds": 0.1,
    }
    watchlist = {
        "enabled": True,
        "interval_seconds": 8,
        "max_concurrency": 1,
        "request_timeout_seconds": 12,
        "jitter_seconds": 0.5,
    }
    return {
        "global": {
            "scan_interval_seconds": None,
            "max_parallel_parsers": 4,
            "request_timeout_seconds": 20,
            "retry_delay_seconds": 0.35,
            "max_retries": 2,
        },
        "sites": {
            "mediamarkt": dict(discovery),
            "dreamland": dict(discovery),
            "bol": dict(discovery),
            "pocketgames": {**discovery, "max_pages": None},
        },
        "watchlist": {
            "enabled": True,
            "backoff_on_429": True,
            "backoff_multiplier": 2.0,
            "max_backoff_seconds": 300,
            "pause_site_on_error": False,
            "sites": {
                "mediamarkt": dict(watchlist),
                "dreamland": {**watchlist, "interval_seconds": 12, "max_concurrency": 2},
                "bol": {**watchlist, "interval_seconds": 12},
                "pocketgames": {**watchlist, "max_concurrency": 2},
            },
        },
    }


def test_int_or_none_parses_ui_string_values():
    assert _int_or_none("25") == 25
    assert _int_or_none(" 3 ") == 3
    assert _int_or_none(4) == 4
    assert _int_or_none("") is None
    assert _int_or_none("nope") is None


def test_public_defaults_do_not_enable_network_notifications_or_actions(tmp_path, monkeypatch):
    keys = (
        "ACTION_MODE",
        "ENABLE_NOTIFICATIONS",
        "ENABLE_BOL",
        "ENABLE_DREAMLAND",
        "ENABLE_MEDIAMARKT",
        "ENABLE_POCKETGAMES",
        "ENABLE_POCKET_GAMES",
        "SELENIUM_PREWARM",
        "SELENIUM_PREWARM_ENABLED",
        "SELENIUM_PREWARM_ON_RUNTIME_START",
        "SELENIUM_KEEP_BROWSER_ALIVE",
        "WATCHLIST_WARM_TABS_ENABLED",
        "MEDIAMARKT_WARM_TABS_ENABLED",
        "WORKER_TELEGRAM_TRACE_ENABLED",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    cfg = load_config(base_dir=tmp_path)

    assert cfg.action_mode == "off"
    assert cfg.enable_notifications is False
    assert cfg.enabled_parser_sites() == ()
    assert cfg.selenium_prewarm is False
    assert cfg.selenium_prewarm_enabled is False
    assert cfg.selenium_prewarm_on_runtime_start is False
    assert cfg.selenium_keep_browser_alive is False
    assert cfg.watchlist_warm_tabs_enabled is False
    assert cfg.mediamarkt_warm_tabs_enabled is False
    assert cfg.worker_telegram_trace_enabled is False


def test_watchlist_interval_config_is_loaded(tmp_path):
    (tmp_path / "scan_settings.json").write_text(
        json.dumps(
            {
                "watchlist": {
                    "enabled": True,
                    "request_timeout_seconds": 7,
                    "jitter_seconds": 0.2,
                    "sites": {"mediamarkt": {"interval_seconds": 5, "max_concurrency": 1}},
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(base_dir=tmp_path)
    assert cfg.watchlist_enabled() is True
    assert cfg.watchlist_site_enabled("mediamarkt") is True
    assert cfg.watchlist_interval_seconds("mediamarkt") == 5
    assert cfg.watchlist_max_concurrency("mediamarkt") == 1
    assert cfg.watchlist_request_timeout_seconds("mediamarkt") == 7
    assert cfg.watchlist_jitter_seconds("mediamarkt") == 0.2


def test_legacy_selenium_prewarm_enables_normalized_runtime_config(tmp_path):
    (tmp_path / ".env").write_text(
        "ACTION_MODE=selenium\nSELENIUM_PREWARM=true\n",
        encoding="utf-8",
    )

    cfg = load_config(base_dir=tmp_path)

    assert cfg.selenium_prewarm is True
    assert cfg.selenium_prewarm_enabled is True
    assert cfg.selenium_prewarm_on_runtime_start is True
    assert cfg.selenium_keep_browser_alive is True
    assert cfg.should_prewarm_selenium_on_runtime_start() is True
    assert cfg.selenium_runtime_config()["prewarm_enabled"] is True


def test_new_selenium_prewarm_fields_override_legacy_value(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ACTION_MODE=selenium",
                "SELENIUM_PREWARM=true",
                "SELENIUM_PREWARM_ENABLED=false",
                "SELENIUM_PREWARM_ON_RUNTIME_START=false",
                "SELENIUM_KEEP_BROWSER_ALIVE=false",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(base_dir=tmp_path)

    assert cfg.selenium_prewarm is True
    assert cfg.selenium_prewarm_enabled is False
    assert cfg.selenium_prewarm_on_runtime_start is False
    assert cfg.selenium_keep_browser_alive is False
    assert cfg.should_prewarm_selenium_on_runtime_start() is False
    assert cfg.selenium_prewarm_skip_reason() == "prewarm_disabled"


def test_challenge_cooldown_config_is_bounded_and_exposed(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "CHALLENGE_COOLDOWN_BASE_SECONDS=12",
                "CHALLENGE_COOLDOWN_MULTIPLIER=3",
                "CHALLENGE_COOLDOWN_MAX_SECONDS=120",
                "CHALLENGE_COOLDOWN_JITTER_RATIO=0.2",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(base_dir=tmp_path)
    snapshot = cfg.selenium_runtime_config()

    assert cfg.challenge_cooldown_base_seconds == 12
    assert cfg.challenge_cooldown_multiplier == 3
    assert cfg.challenge_cooldown_max_seconds == 120
    assert cfg.challenge_cooldown_jitter_ratio == 0.2
    assert snapshot["challenge_cooldown_max_seconds"] == 120


def test_scan_settings_api_saves_watchlist_settings(tmp_path):
    manager = ConfigManager(tmp_path)
    payload = _scan_settings_payload()
    payload["watchlist"]["sites"]["mediamarkt"]["enabled"] = False
    payload["watchlist"]["sites"]["mediamarkt"]["interval_seconds"] = 3
    payload["watchlist"]["sites"]["mediamarkt"]["request_timeout_seconds"] = 9
    response = manager.save_scan_settings(payload)

    saved = json.loads((tmp_path / "scan_settings.json").read_text(encoding="utf-8"))
    assert saved["watchlist"]["sites"]["mediamarkt"]["enabled"] is False
    assert saved["watchlist"]["sites"]["mediamarkt"]["interval_seconds"] == 3
    assert saved["watchlist"]["sites"]["mediamarkt"]["request_timeout_seconds"] == 9
    assert response["watchlist"]["sites"]["mediamarkt"]["enabled"] is False
    assert response["watchlist"]["sites"]["mediamarkt"]["interval_seconds"] == 3


def test_scan_settings_api_rejects_watchlist_backoff_below_interval(tmp_path):
    manager = ConfigManager(tmp_path)
    payload = _scan_settings_payload()
    payload["watchlist"]["max_backoff_seconds"] = 4
    payload["watchlist"]["sites"]["mediamarkt"]["interval_seconds"] = 8

    with pytest.raises(ValueError, match="max_backoff_seconds"):
        manager.save_scan_settings(payload)


def test_sensitive_credentials_are_write_only_and_blank_update_preserves_value(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_BOT_TOKEN=fixture-token\n", encoding="utf-8")
    manager = ConfigManager(tmp_path)

    response = manager.get_credentials()
    token = next(item for item in response["items"] if item["key"] == "TELEGRAM_BOT_TOKEN")
    assert token["value"] == ""
    assert token["configured"] is True

    manager.save_credentials({"TELEGRAM_BOT_TOKEN": "", "TIMEZONE": "Europe/Amsterdam"})
    saved = env_path.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=fixture-token" in saved
    assert "TIMEZONE=Europe/Amsterdam" in saved


def test_personal_checkout_and_telegram_values_are_write_only(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "CHECKOUT_EMAIL=owner@example.com\n"
        "CHECKOUT_FIRST_NAME=Owner\n"
        "CHECKOUT_STREET=PrivateStreet\n"
        "TELEGRAM_CHAT_ID=123456\n"
        "TELEGRAM_BOT_TOKEN=fixture-token\n",
        encoding="utf-8",
    )
    manager = ConfigManager(tmp_path)

    credentials = {item["key"]: item for item in manager.get_credentials()["items"]}
    for key in ("CHECKOUT_EMAIL", "CHECKOUT_FIRST_NAME", "CHECKOUT_STREET", "TELEGRAM_CHAT_ID"):
        assert credentials[key]["value"] == ""
        assert credentials[key]["configured"] is True

    telegram = manager.get_telegram_settings()
    assert telegram["bot_token"] == ""
    assert telegram["chat_id"] == ""
    assert telegram["configured"] is True

    manager.save_telegram_settings({"bot_token": "", "chat_id": ""})
    saved = env_path.read_text(encoding="utf-8")
    assert "TELEGRAM_CHAT_ID=123456" in saved
    assert "TELEGRAM_BOT_TOKEN=fixture-token" in saved


def test_env_writer_rejects_control_character_injection_without_changing_file(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("TIMEZONE=UTC\n", encoding="utf-8")
    manager = ConfigManager(tmp_path)

    with pytest.raises(ValueError, match="control characters"):
        manager.save_credentials({"TIMEZONE": "UTC\nACTION_MODE=selenium"})

    assert env_path.read_text(encoding="utf-8") == "TIMEZONE=UTC\n"


def test_concurrent_env_updates_do_not_lose_unrelated_fields(tmp_path):
    manager = ConfigManager(tmp_path)
    (tmp_path / ".env").write_text("ACTION_MODE=off\nTIMEZONE=UTC\n", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager._write_env_updates, {"ACTION_MODE": "notify_only"})
        second = pool.submit(manager._write_env_updates, {"TIMEZONE": "Europe/Amsterdam"})
        first.result()
        second.result()

    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ACTION_MODE=notify_only" in saved
    assert "TIMEZONE=Europe/Amsterdam" in saved
