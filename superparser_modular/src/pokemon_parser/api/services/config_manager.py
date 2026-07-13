from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values

from pokemon_parser.config import (
    AppConfig,
    DEFAULT_SCAN_GLOBALS,
    SCAN_SETTINGS_FILENAME,
    SITE_SCAN_DEFAULTS,
    WATCHLIST_DEFAULTS,
    WORKER_SPEED_PROFILES,
    load_config,
    parse_bool,
)
from pokemon_parser.notifications.telegram import TelegramNotifier
from pokemon_parser.parsers import SITE_LABELS
from pokemon_parser.utils.proxy import build_requests_proxy_map
from pokemon_parser.utils.time import utc_now_iso

SAFE_RAW_ENV_RE = re.compile(r"^[A-Za-z0-9_./:@%+\-,]+$")
logger = logging.getLogger(__name__)
CONFIG_WRITE_LOCK = threading.RLock()


@dataclass(frozen=True)
class ConfigFieldSpec:
    key: str
    label: str
    group: str
    field_type: str = "text"
    sensitive: bool = False
    default: Any = ""


class ConfigManager:
    PARSER_ENV_KEYS = {
        "mediamarkt": ("ENABLE_MEDIAMARKT",),
        "dreamland": ("ENABLE_DREAMLAND",),
        "bol": ("ENABLE_BOL",),
        "pocketgames": ("ENABLE_POCKETGAMES", "ENABLE_POCKET_GAMES"),
    }

    CREDENTIAL_FIELDS = (
        ConfigFieldSpec("TIMEZONE", "Timezone", "runtime", default="Europe/Amsterdam"),
        ConfigFieldSpec("ACTION_MODE", "Action Mode", "runtime", default="off"),
        ConfigFieldSpec("SNAPSHOT_MODE", "Snapshot Mode", "runtime", default="changes"),
        ConfigFieldSpec("PARSER_CONCURRENCY", "Parser Concurrency", "runtime", field_type="number", default=4),
        ConfigFieldSpec(
            "HEARTBEAT_INTERVAL_SECONDS",
            "Heartbeat Interval (s)",
            "runtime",
            field_type="number",
            default=3600,
        ),
        ConfigFieldSpec("ENABLE_MEDIAMARKT", "Enable MediaMarkt", "runtime", field_type="boolean", default=True),
        ConfigFieldSpec("ENABLE_DREAMLAND", "Enable Dreamland", "runtime", field_type="boolean", default=True),
        ConfigFieldSpec("ENABLE_BOL", "Enable Bol", "runtime", field_type="boolean", default=True),
        ConfigFieldSpec("ENABLE_POCKETGAMES", "Enable PocketGames", "runtime", field_type="boolean", default=True),
        ConfigFieldSpec("TELEGRAM_BOT_TOKEN", "Bot Token", "telegram", sensitive=True),
        ConfigFieldSpec("TELEGRAM_CHAT_ID", "Chat ID", "telegram", sensitive=True),
        ConfigFieldSpec("CHROME_BINARY", "Chrome Binary", "selenium"),
        ConfigFieldSpec("CHROME_USER_DATA_DIR", "Chrome User Data Dir", "selenium"),
        ConfigFieldSpec("CHROME_PROFILE_DIR", "Chrome Profile Dir", "selenium"),
        ConfigFieldSpec("SELENIUM_TEST_URL", "Selenium Test URL", "selenium"),
        ConfigFieldSpec("SELENIUM_PREWARM", "Selenium Prewarm", "selenium", field_type="boolean", default=True),
        ConfigFieldSpec("SELENIUM_PREWARM_ENABLED", "Selenium Prewarm Enabled", "selenium", field_type="boolean", default=True),
        ConfigFieldSpec("SELENIUM_PREWARM_ON_RUNTIME_START", "Prewarm On Runtime Start", "selenium", field_type="boolean", default=True),
        ConfigFieldSpec("SELENIUM_KEEP_BROWSER_ALIVE", "Keep Browser Alive", "selenium", field_type="boolean", default=True),
        ConfigFieldSpec("WATCHLIST_WARM_TABS_ENABLED", "Watchlist Warm Tabs", "selenium", field_type="boolean", default=True),
        ConfigFieldSpec("WATCHLIST_WARM_TABS_MAX", "Watchlist Warm Tabs Max", "selenium", field_type="number", default=6),
        ConfigFieldSpec("WATCHLIST_WARM_TAB_REFRESH_INTERVAL_SECONDS", "Warm Tab Refresh Seconds", "selenium", field_type="number", default=30),
        ConfigFieldSpec("WATCHLIST_WARM_TAB_MIN_REFRESH_INTERVAL_SECONDS", "Warm Tab Min Refresh Seconds", "selenium", field_type="number", default=15),
        ConfigFieldSpec("WATCHLIST_WARM_TAB_RELOAD_TIMEOUT_SECONDS", "Warm Tab Reload Timeout", "selenium", field_type="number", default=8),
        ConfigFieldSpec("WATCHLIST_WARM_TAB_STALE_AFTER_SECONDS", "Warm Tab Stale Seconds", "selenium", field_type="number", default=60),
        ConfigFieldSpec("MEDIAMARKT_WARM_TABS_ENABLED", "MediaMarkt Warm Tabs", "selenium", field_type="boolean", default=True),
        ConfigFieldSpec("BOL_BUY_NOW_URL", "Bol Buy Now URL", "selenium"),
        ConfigFieldSpec("CHECKOUT_EMAIL", "Checkout Email", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_FIRST_NAME", "First Name", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_LAST_NAME", "Last Name", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_STREET", "Street", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_HOUSE_NUMBER", "House Number", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_ZIP_CODE", "ZIP Code", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_CITY", "City", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_CARD_NUMBER", "Card Number", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_CARD_EXPIRY", "Card Expiry", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_CARD_CVV", "Card CVV", "checkout", sensitive=True),
        ConfigFieldSpec("CHECKOUT_CARD_NAME", "Card Name", "checkout", sensitive=True),
    )

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).resolve()
        self.env_path = self.base_dir / ".env"
        self.scan_settings_path = self.base_dir / SCAN_SETTINGS_FILENAME

    def load_app_config(self) -> AppConfig:
        return load_config(base_dir=self.base_dir)

    def _load_env_map(self) -> dict[str, Any]:
        if not self.env_path.exists():
            return {}
        return dict(dotenv_values(self.env_path))

    @staticmethod
    def _serialize_env_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return '""'

        text = str(value)
        if any(character in text for character in ("\r", "\n", "\x00")):
            raise ValueError("Configuration values must not contain control characters.")
        if text == "":
            return '""'
        if SAFE_RAW_ENV_RE.match(text):
            return text
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _parse_bool_value(value: Any, default: bool = False) -> bool:
        return parse_bool(None if value is None else str(value), default)

    @staticmethod
    def _parse_int_value(value: Any, default: int = 0, minimum: int | None = None) -> int:
        try:
            parsed = int(str(value).strip())
        except Exception:
            parsed = default
        if minimum is not None:
            parsed = max(minimum, parsed)
        return parsed

    @staticmethod
    def _parse_float_value(value: Any, default: float = 0.0, minimum: float | None = None) -> float:
        try:
            parsed = float(str(value).strip())
        except Exception:
            parsed = default
        if minimum is not None:
            parsed = max(minimum, parsed)
        return parsed

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                temporary_path = Path(stream.name)
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _write_env_updates(self, updates: dict[str, Any]) -> None:
        normalized = {key: self._serialize_env_value(value) for key, value in updates.items()}
        with CONFIG_WRITE_LOCK:
            existing_lines = self.env_path.read_text(encoding="utf-8").splitlines() if self.env_path.exists() else []
            seen: set[str] = set()
            output: list[str] = []

            for line in existing_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in line:
                    output.append(line)
                    continue

                key, _ = line.split("=", 1)
                key = key.strip()
                if key in normalized:
                    output.append(f"{key}={normalized[key]}")
                    seen.add(key)
                else:
                    output.append(line)

            for key, value in normalized.items():
                if key not in seen:
                    output.append(f"{key}={value}")

            self._atomic_write_text(self.env_path, "\n".join(output).rstrip() + "\n")

    def _load_scan_settings_map(self) -> dict[str, Any]:
        if not self.scan_settings_path.exists():
            return {}
        try:
            payload = json.loads(self.scan_settings_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_scan_settings_map(self, payload: dict[str, Any]) -> None:
        with CONFIG_WRITE_LOCK:
            self._atomic_write_text(
                self.scan_settings_path,
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )

    def _credential_value(self, env_map: dict[str, Any], spec: ConfigFieldSpec) -> Any:
        raw_value = env_map.get(spec.key, spec.default)
        if spec.field_type == "boolean":
            return self._parse_bool_value(raw_value, bool(spec.default))
        if spec.field_type == "number":
            return self._parse_int_value(raw_value, int(spec.default or 0))
        return "" if raw_value is None else str(raw_value)

    def get_credentials(self) -> dict[str, Any]:
        env_map = self._load_env_map()
        items = [
            {
                "key": spec.key,
                "label": spec.label,
                "group": spec.group,
                "field_type": spec.field_type,
                "sensitive": spec.sensitive,
                "value": "" if spec.sensitive else self._credential_value(env_map, spec),
                "configured": bool(self._credential_value(env_map, spec)) if spec.sensitive else None,
            }
            for spec in self.CREDENTIAL_FIELDS
        ]
        return {
            "items": items,
            "updated_at": utc_now_iso(),
        }

    def save_credentials(self, values: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        for spec in self.CREDENTIAL_FIELDS:
            if spec.key not in values:
                continue
            if spec.sensitive and not str(values.get(spec.key) or "").strip():
                continue
            if spec.field_type == "boolean":
                updates[spec.key] = self._parse_bool_value(values[spec.key], bool(spec.default))
            elif spec.field_type == "number":
                updates[spec.key] = self._parse_int_value(values[spec.key], int(spec.default or 0))
            else:
                updates[spec.key] = "" if values[spec.key] is None else str(values[spec.key]).strip()

        if updates:
            self._write_env_updates(updates)
        return self.get_credentials()

    def reload_credentials(self) -> dict[str, Any]:
        return self.get_credentials()

    def get_parser_settings(self) -> dict[str, bool]:
        cfg = self.load_app_config()
        return cfg.parser_enabled_map()

    def toggle_parser(self, site: str) -> dict[str, bool]:
        site_key = site.strip().lower()
        if site_key not in self.PARSER_ENV_KEYS:
            raise ValueError(f"unknown parser site={site}")

        current = self.get_parser_settings()
        new_value = not bool(current.get(site_key, False))
        updates = {env_key: new_value for env_key in self.PARSER_ENV_KEYS[site_key]}
        self._write_env_updates(updates)
        return self.get_parser_settings()

    def get_proxy_settings(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        return {
            "enabled": cfg.proxy_enabled,
            "type": cfg.proxy_type,
            "host": cfg.proxy_host,
            "port": cfg.proxy_port,
            "login": "",
            "password": "",
            "login_configured": bool(cfg.proxy_login),
            "has_credentials": bool(cfg.proxy_login and cfg.proxy_password),
            "url": f"{cfg.proxy_type}://{cfg.proxy_host}:{cfg.proxy_port}" if cfg.proxy_host and cfg.proxy_port else None,
        }

    def save_proxy_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        updates = {
            "PROXY_ENABLED": bool(payload.get("enabled", False)),
            "PROXY_TYPE": str(payload.get("type", "http") or "http").lower(),
            "PROXY_HOST": str(payload.get("host", "") or "").strip(),
            "PROXY_PORT": self._parse_int_value(payload.get("port", 0), 0, minimum=0),
        }
        login = str(payload.get("login", "") or "").strip()
        if login:
            updates["PROXY_LOGIN"] = login
        password = str(payload.get("password", "") or "").strip()
        if password:
            updates["PROXY_PASSWORD"] = password
        self._write_env_updates(updates)
        return self.get_proxy_settings()

    def test_proxy(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        stored = self.load_app_config()
        candidate = {
            "enabled": stored.proxy_enabled,
            "type": stored.proxy_type,
            "host": stored.proxy_host,
            "port": stored.proxy_port,
            "login": stored.proxy_login,
            "password": stored.proxy_password,
        } if payload is None else {
            "enabled": bool(payload.get("enabled", False)),
            "type": str(payload.get("type", "http") or "http").lower(),
            "host": str(payload.get("host", "") or "").strip(),
            "port": self._parse_int_value(payload.get("port", 0), 0, minimum=0),
            "login": str(payload.get("login", "") or "").strip(),
            "password": str(payload.get("password", "") or stored.proxy_password or "").strip(),
        }
        if not candidate["enabled"]:
            return {"ok": False, "message": "Proxy is disabled."}

        class ProxyConfigShim:
            proxy_enabled = candidate["enabled"]
            proxy_type = candidate["type"]
            proxy_host = candidate["host"]
            proxy_port = candidate["port"]
            proxy_login = candidate["login"]
            proxy_password = candidate["password"]

        response = requests.get(
            "https://httpbin.org/ip",
            timeout=15,
            proxies=build_requests_proxy_map(ProxyConfigShim),
        )
        response.raise_for_status()
        return {
            "ok": True,
            "message": "Proxy connection succeeded.",
            "status_code": response.status_code,
            "result": response.json(),
        }

    def get_telegram_settings(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        return {
            "bot_token": "",
            "chat_id": "",
            "configured": bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
            "token_configured": bool(cfg.telegram_bot_token),
            "chat_id_configured": bool(cfg.telegram_chat_id),
        }

    def save_telegram_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        chat_id = str(payload.get("chat_id", "") or "").strip()
        if chat_id:
            updates["TELEGRAM_CHAT_ID"] = chat_id
        token = str(payload.get("bot_token", "") or "").strip()
        if token:
            updates["TELEGRAM_BOT_TOKEN"] = token
        if updates:
            self._write_env_updates(updates)
        return self.get_telegram_settings()

    def send_telegram_test(self, text: str | None = None) -> dict[str, Any]:
        cfg = self.load_app_config()
        notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
        if not notifier.is_enabled():
            return {"ok": False, "message": "Telegram bot token or chat id is missing."}
        notifier.send_sync(
            text or f"Pokemon Parser dashboard test message ({utc_now_iso()})",
            proxy_cfg=cfg,
            timeout=15.0,
        )
        return {"ok": True, "message": "Telegram test message sent."}

    def get_notifications_settings(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        return {
            "enabled": cfg.enable_notifications,
            "heartbeat_alerts": cfg.enable_heartbeat_alerts,
            "success_alerts": cfg.enable_success_alerts,
            "error_alerts": cfg.enable_error_alerts,
            "worker_trace_enabled": cfg.worker_telegram_trace_enabled,
            "worker_trace_level": cfg.worker_telegram_trace_level,
            "worker_trace_queue_update_seconds": cfg.worker_trace_queue_update_seconds,
        }

    def save_notifications_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        trace_level = str(payload.get("worker_trace_level", "normal") or "normal").strip().lower()
        if trace_level not in {"minimal", "normal", "verbose"}:
            trace_level = "normal"
        queue_update_seconds = self._parse_float_value(
            payload.get("worker_trace_queue_update_seconds", 60.0),
            60.0,
            minimum=5.0,
        )
        if queue_update_seconds > 3600:
            raise ValueError("worker_trace_queue_update_seconds must be between 5 and 3600.")

        self._write_env_updates(
            {
                "ENABLE_NOTIFICATIONS": bool(payload.get("enabled", False)),
                "ENABLE_HEARTBEAT_ALERTS": bool(payload.get("heartbeat_alerts", False)),
                "ENABLE_SUCCESS_ALERTS": bool(payload.get("success_alerts", False)),
                "ENABLE_ERROR_ALERTS": bool(payload.get("error_alerts", False)),
                "WORKER_TELEGRAM_TRACE_ENABLED": bool(payload.get("worker_trace_enabled", False)),
                "WORKER_TELEGRAM_TRACE_LEVEL": trace_level,
                "WORKER_TRACE_QUEUE_UPDATE_SECONDS": queue_update_seconds,
            }
        )
        return self.get_notifications_settings()

    def get_worker_settings(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        from pokemon_parser.workers.timing import build_worker_timing

        timing = build_worker_timing(cfg)
        return {
            "queue_check_enabled": cfg.queue_check_enabled,
            "queue_wait_timeout_seconds": cfg.queue_wait_timeout_seconds,
            "queue_poll_seconds": cfg.queue_poll_seconds,
            "worker_speed_profile": cfg.worker_speed_profile,
            "worker_speed_profile_options": ["safe", "balanced", "fast", "custom"],
            "worker_click_pause_seconds": timing.click_pause_seconds,
            "worker_after_navigation_wait_seconds": timing.after_navigation_wait_seconds,
            "worker_after_add_to_cart_wait_seconds": timing.after_add_to_cart_wait_seconds,
            "worker_after_checkout_click_wait_seconds": timing.after_checkout_click_wait_seconds,
            "worker_wait_timeout_seconds": timing.wait_timeout_seconds,
            "worker_poll_seconds": timing.poll_seconds,
            "worker_retry_pause_seconds": timing.retry_pause_seconds,
        }

    def save_worker_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = str(payload.get("worker_speed_profile", "balanced") or "balanced").strip().lower()
        if profile not in WORKER_SPEED_PROFILES:
            profile = "balanced"

        queue_wait = self._parse_float_value(payload.get("queue_wait_timeout_seconds", 300.0), 300.0, minimum=1.0)
        if queue_wait > 7200:
            raise ValueError("queue_wait_timeout_seconds must be between 1 and 7200.")

        queue_poll = self._parse_float_value(payload.get("queue_poll_seconds", 1.0), 1.0, minimum=0.1)
        if queue_poll > 60:
            raise ValueError("queue_poll_seconds must be between 0.1 and 60.")

        wait_timeout = self._parse_float_value(payload.get("worker_wait_timeout_seconds", 20.0), 20.0, minimum=1.0)
        if wait_timeout > 120:
            raise ValueError("worker_wait_timeout_seconds must be between 1 and 120.")

        poll_seconds = self._parse_float_value(payload.get("worker_poll_seconds", 0.2), 0.2, minimum=0.05)
        if poll_seconds > 5:
            raise ValueError("worker_poll_seconds must be between 0.05 and 5.")

        click_pause = self._parse_float_value(payload.get("worker_click_pause_seconds", 0.2), 0.2, minimum=0.0)
        nav_wait = self._parse_float_value(payload.get("worker_after_navigation_wait_seconds", 0.5), 0.5, minimum=0.0)
        add_wait = self._parse_float_value(payload.get("worker_after_add_to_cart_wait_seconds", 0.6), 0.6, minimum=0.0)
        checkout_wait = self._parse_float_value(
            payload.get("worker_after_checkout_click_wait_seconds", 0.6),
            0.6,
            minimum=0.0,
        )
        retry_pause = self._parse_float_value(payload.get("worker_retry_pause_seconds", 0.45), 0.45, minimum=0.0)

        for key, value in {
            "worker_click_pause_seconds": click_pause,
            "worker_after_navigation_wait_seconds": nav_wait,
            "worker_after_add_to_cart_wait_seconds": add_wait,
            "worker_after_checkout_click_wait_seconds": checkout_wait,
            "worker_retry_pause_seconds": retry_pause,
        }.items():
            if value > 30:
                raise ValueError(f"{key} must be between 0 and 30.")

        self._write_env_updates(
            {
                "QUEUE_CHECK_ENABLED": bool(payload.get("queue_check_enabled", True)),
                "QUEUE_WAIT_TIMEOUT_SECONDS": queue_wait,
                "QUEUE_POLL_SECONDS": queue_poll,
                "WORKER_SPEED_PROFILE": profile,
                "WORKER_CLICK_PAUSE_SECONDS": click_pause,
                "WORKER_AFTER_NAVIGATION_WAIT_SECONDS": nav_wait,
                "WORKER_AFTER_ADD_TO_CART_WAIT_SECONDS": add_wait,
                "WORKER_AFTER_CHECKOUT_CLICK_WAIT_SECONDS": checkout_wait,
                "WORKER_WAIT_TIMEOUT_SECONDS": wait_timeout,
                "WORKER_POLL_SECONDS": poll_seconds,
                "WORKER_RETRY_PAUSE_SECONDS": retry_pause,
            }
        )
        return self.get_worker_settings()

    def get_action_mode_settings(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        return {
            "mode": cfg.action_mode,
            "options": ["off", "notify_only", "selenium"],
        }

    def save_action_mode_settings(self, mode: str) -> dict[str, Any]:
        normalized = str(mode or "notify_only").strip().lower()
        if normalized == "telegram":
            normalized = "notify_only"
        if normalized not in {"off", "notify_only", "selenium"}:
            normalized = "notify_only"
        self._write_env_updates({"ACTION_MODE": normalized})
        return self.get_action_mode_settings()

    def get_timer_settings(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        return {
            "enabled": cfg.timer_enabled,
            "interval": cfg.timer_interval,
            "unit": cfg.timer_unit,
            "interval_seconds": cfg.timer_interval_seconds(),
        }

    def save_timer_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        interval = self._parse_int_value(payload.get("interval", 15), 15, minimum=1)
        unit = str(payload.get("unit", "minutes") or "minutes").strip().lower()
        if unit not in {"seconds", "minutes", "hours"}:
            unit = "minutes"
        self._write_env_updates(
            {
                "ENABLE_TIMER": bool(payload.get("enabled", False)),
                "TIMER_INTERVAL": interval,
                "TIMER_UNIT": unit,
            }
        )
        return self.get_timer_settings()

    def get_config_status(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        proxy_complete = not cfg.proxy_enabled or bool(cfg.proxy_host and cfg.proxy_port > 0)
        telegram_configured = bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
        enabled_sites = cfg.enabled_parser_sites()

        checks = [
            {
                "key": "env_loaded",
                "label": ".env file loaded",
                "ok": self.env_path.exists(),
            },
            {
                "key": "db_path",
                "label": "SQLite path resolved",
                "ok": bool(cfg.resolved_db_path()),
            },
            {
                "key": "parsers",
                "label": "At least one parser enabled",
                "ok": bool(enabled_sites),
            },
            {
                "key": "telegram",
                "label": "Telegram settings configured",
                "ok": telegram_configured,
            },
            {
                "key": "proxy",
                "label": "Proxy configuration valid",
                "ok": proxy_complete,
            },
        ]

        missing_required_fields: list[str] = []
        if not self.env_path.exists():
            missing_required_fields.append(".env")
        if not enabled_sites:
            missing_required_fields.append("ENABLE_* parser toggles")
        if cfg.enable_notifications and not telegram_configured:
            missing_required_fields.extend(["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
        if cfg.proxy_enabled and not proxy_complete:
            missing_required_fields.extend(["PROXY_HOST", "PROXY_PORT"])

        return {
            "env_loaded": self.env_path.exists(),
            "all_required_ok": all(check["ok"] for check in checks if check["key"] != "telegram" or cfg.enable_notifications),
            "action_mode": cfg.action_mode,
            "timezone": cfg.timezone_name,
            "snapshot_mode": cfg.snapshot_mode,
            "missing_required_fields": missing_required_fields,
            "checks": checks,
            "enabled_parsers": list(enabled_sites),
            "parser_labels": SITE_LABELS,
            "notifications": self.get_notifications_settings(),
            "proxy_enabled": cfg.proxy_enabled,
        }

    def _scan_settings_site_payload(self, cfg: AppConfig, site: str) -> dict[str, Any]:
        defaults = SITE_SCAN_DEFAULTS.get(site, SITE_SCAN_DEFAULTS["pocketgames"])
        site_map = cfg.scan_settings.get("sites", {}).get(site, {}) if isinstance(cfg.scan_settings, dict) else {}
        site_map = site_map if isinstance(site_map, dict) else {}
        return {
            "site": site,
            "label": SITE_LABELS.get(site, site.title()),
            "enabled": cfg.is_parser_enabled(site),
            "scan_delay_seconds": round(cfg.effective_scan_delay_seconds(site), 3),
            "cooldown_seconds": round(cfg.site_cooldown_seconds(site), 3),
            "request_timeout_seconds": round(cfg.site_request_timeout_seconds(site), 3),
            "max_pages": cfg.site_max_pages(site),
            "page_delay_seconds": round(cfg.site_page_delay_seconds(site), 3),
            "supports_max_pages": defaults.get("max_pages") is not None or site == "pocketgames",
            "supports_page_delay_seconds": True,
            "uses_saved_scan_delay": "scan_delay_seconds" in site_map,
            **(
                {
                    "mediamarkt_graphql_backoff_seconds": round(cfg.mediamarkt_graphql_backoff_seconds(), 3),
                    "mediamarkt_graphql_backoff_multiplier": round(cfg.mediamarkt_graphql_backoff_multiplier(), 3),
                    "mediamarkt_graphql_max_backoff_seconds": round(
                        cfg.mediamarkt_graphql_max_backoff_seconds(),
                        3,
                    ),
                }
                if site == "mediamarkt"
                else {}
            ),
        }

    def _watchlist_settings_site_payload(self, cfg: AppConfig, site: str) -> dict[str, Any]:
        return {
            "site": site,
            "label": SITE_LABELS.get(site, site.title()),
            "enabled": cfg.watchlist_site_enabled(site),
            "interval_seconds": round(cfg.watchlist_interval_seconds(site), 3),
            "max_concurrency": cfg.watchlist_max_concurrency(site),
            "request_timeout_seconds": round(cfg.watchlist_request_timeout_seconds(site), 3),
            "jitter_seconds": round(cfg.watchlist_jitter_seconds(site), 3),
            "discovery_scan_delay_seconds": round(cfg.effective_scan_delay_seconds(site), 3),
        }

    def get_scan_settings(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        global_map = cfg.scan_settings.get("global", {}) if isinstance(cfg.scan_settings, dict) else {}
        global_map = global_map if isinstance(global_map, dict) else {}
        sites = {
            site: self._scan_settings_site_payload(cfg, site)
            for site in SITE_LABELS
        }
        enabled_count = max(1, len(cfg.enabled_parser_sites()))
        return {
            "global": {
                "scan_interval_seconds": global_map.get("scan_interval_seconds"),
                "scan_interval_default_seconds": DEFAULT_SCAN_GLOBALS["scan_interval_seconds"],
                "max_parallel_parsers": cfg.parser_concurrency,
                "effective_parallel_parsers": min(cfg.parser_concurrency, enabled_count),
                "request_timeout_seconds": round(cfg.request_timeout_override_seconds() or DEFAULT_SCAN_GLOBALS["request_timeout_seconds"], 3),
                "retry_delay_seconds": round(cfg.retry_delay_seconds(), 3),
                "max_retries": cfg.max_retries(),
                "using_legacy_interval_defaults": global_map.get("scan_interval_seconds") in {None, ""},
            },
            "sites": sites,
            "watchlist": {
                "enabled": cfg.watchlist_enabled(),
                "backoff_on_429": cfg.watchlist_backoff_on_429(),
                "backoff_multiplier": cfg.watchlist_backoff_multiplier(),
                "max_backoff_seconds": cfg.watchlist_max_backoff_seconds(),
                "pause_site_on_error": cfg.watchlist_pause_site_on_error(),
                "warm_tabs_enabled": cfg.watchlist_warm_tabs_enabled,
                "warm_tabs_max": cfg.watchlist_warm_tabs_max,
                "warm_tab_refresh_interval_seconds": cfg.watchlist_warm_tab_refresh_interval_seconds,
                "warm_tab_min_refresh_interval_seconds": cfg.watchlist_warm_tab_min_refresh_interval_seconds,
                "warm_tab_reload_timeout_seconds": cfg.watchlist_warm_tab_reload_timeout_seconds,
                "warm_tab_stale_after_seconds": cfg.watchlist_warm_tab_stale_after_seconds,
                "mediamarkt_warm_tabs_enabled": cfg.mediamarkt_warm_tabs_enabled,
                "sites": {
                    site: self._watchlist_settings_site_payload(cfg, site)
                    for site in SITE_LABELS
                },
            },
            "storage": {
                "path": str(self.scan_settings_path),
                "exists": self.scan_settings_path.exists(),
            },
        }

    def get_scan_settings_effective(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        payload = self.get_scan_settings()
        payload["effective"] = {
            "enabled_sites": list(cfg.enabled_parser_sites()),
            "effective_parallel_parsers": cfg.effective_parallel_parsers(),
            "watchlist_enabled": cfg.watchlist_enabled(),
            "watchlist_enabled_sites": [
                site for site in cfg.enabled_parser_sites() if cfg.watchlist_site_enabled(site)
            ],
            "watchlist_warm_tabs_enabled": cfg.watchlist_warm_tabs_enabled,
            "watchlist_warm_tabs_max": cfg.watchlist_warm_tabs_max,
        }
        return payload

    def save_scan_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current_cfg = self.load_app_config()
        current_enabled = current_cfg.parser_enabled_map()
        global_payload = payload.get("global", {}) if isinstance(payload.get("global"), dict) else {}
        site_payloads = payload.get("sites", {}) if isinstance(payload.get("sites"), dict) else {}
        watchlist_payload = payload.get("watchlist", {}) if isinstance(payload.get("watchlist"), dict) else {}
        watchlist_site_payloads = (
            watchlist_payload.get("sites", {}) if isinstance(watchlist_payload.get("sites"), dict) else {}
        )

        env_updates: dict[str, Any] = {}
        file_payload: dict[str, Any] = {
            "global": {},
            "sites": {},
        }

        max_parallel = self._parse_int_value(
            global_payload.get("max_parallel_parsers", current_cfg.parser_concurrency),
            current_cfg.parser_concurrency,
            minimum=1,
        )
        if max_parallel > len(SITE_LABELS):
            raise ValueError(f"max_parallel_parsers must be between 1 and {len(SITE_LABELS)}")
        env_updates["PARSER_CONCURRENCY"] = max_parallel

        scan_interval_raw = global_payload.get("scan_interval_seconds")
        if scan_interval_raw not in {None, ""}:
            scan_interval = self._parse_float_value(scan_interval_raw, 0.0, minimum=0.0)
            if scan_interval <= 0:
                raise ValueError("scan_interval_seconds must be greater than 0 when provided.")
            if scan_interval > 3600:
                raise ValueError("scan_interval_seconds must be 3600 seconds or less.")
            file_payload["global"]["scan_interval_seconds"] = scan_interval

        request_timeout_seconds = self._parse_float_value(
            global_payload.get("request_timeout_seconds", DEFAULT_SCAN_GLOBALS["request_timeout_seconds"]),
            float(DEFAULT_SCAN_GLOBALS["request_timeout_seconds"]),
            minimum=0.0,
        )
        if request_timeout_seconds <= 0 or request_timeout_seconds > 120:
            raise ValueError("request_timeout_seconds must be between 1 and 120.")
        file_payload["global"]["request_timeout_seconds"] = request_timeout_seconds

        retry_delay_seconds = self._parse_float_value(
            global_payload.get("retry_delay_seconds", DEFAULT_SCAN_GLOBALS["retry_delay_seconds"]),
            float(DEFAULT_SCAN_GLOBALS["retry_delay_seconds"]),
            minimum=0.0,
        )
        if retry_delay_seconds < 0 or retry_delay_seconds > 60:
            raise ValueError("retry_delay_seconds must be between 0 and 60.")
        file_payload["global"]["retry_delay_seconds"] = retry_delay_seconds

        max_retries = self._parse_int_value(
            global_payload.get("max_retries", DEFAULT_SCAN_GLOBALS["max_retries"]),
            int(DEFAULT_SCAN_GLOBALS["max_retries"]),
            minimum=0,
        )
        if max_retries > 10:
            raise ValueError("max_retries must be 10 or less.")
        file_payload["global"]["max_retries"] = max_retries

        for site in SITE_LABELS:
            site_payload = site_payloads.get(site, {}) if isinstance(site_payloads.get(site), dict) else {}
            enabled = bool(site_payload.get("enabled", current_enabled.get(site, True)))
            for env_key in self.PARSER_ENV_KEYS[site]:
                env_updates[env_key] = enabled

            scan_delay_seconds = self._parse_float_value(
                site_payload.get("scan_delay_seconds", current_cfg.effective_scan_delay_seconds(site)),
                current_cfg.effective_scan_delay_seconds(site),
                minimum=0.0,
            )
            if scan_delay_seconds <= 0 or scan_delay_seconds > 3600:
                raise ValueError(f"{site}: scan_delay_seconds must be between 0 and 3600.")

            cooldown_seconds = self._parse_float_value(
                site_payload.get("cooldown_seconds", current_cfg.site_cooldown_seconds(site)),
                current_cfg.site_cooldown_seconds(site),
                minimum=0.0,
            )
            if cooldown_seconds < 1 or cooldown_seconds > 7200:
                raise ValueError(f"{site}: cooldown_seconds must be between 1 and 7200.")

            site_timeout = self._parse_float_value(
                site_payload.get("request_timeout_seconds", current_cfg.site_request_timeout_seconds(site)),
                current_cfg.site_request_timeout_seconds(site),
                minimum=0.0,
            )
            if site_timeout <= 0 or site_timeout > 120:
                raise ValueError(f"{site}: request_timeout_seconds must be between 1 and 120.")

            max_pages_raw = site_payload.get("max_pages")
            max_pages = None if max_pages_raw in {None, ""} else self._parse_int_value(max_pages_raw, 0, minimum=1)
            if max_pages is not None and max_pages > 200:
                raise ValueError(f"{site}: max_pages must be 200 or less.")

            page_delay_seconds = self._parse_float_value(
                site_payload.get("page_delay_seconds", current_cfg.site_page_delay_seconds(site)),
                current_cfg.site_page_delay_seconds(site),
                minimum=0.0,
            )
            if page_delay_seconds < 0 or page_delay_seconds > 60:
                raise ValueError(f"{site}: page_delay_seconds must be between 0 and 60.")

            site_entry = {
                "scan_delay_seconds": scan_delay_seconds,
                "cooldown_seconds": cooldown_seconds,
                "request_timeout_seconds": site_timeout,
                "page_delay_seconds": page_delay_seconds,
            }
            if max_pages is not None:
                site_entry["max_pages"] = max_pages
            if site == "mediamarkt":
                graphql_backoff_seconds = self._parse_float_value(
                    site_payload.get(
                        "mediamarkt_graphql_backoff_seconds",
                        current_cfg.mediamarkt_graphql_backoff_seconds(),
                    ),
                    current_cfg.mediamarkt_graphql_backoff_seconds(),
                    minimum=1.0,
                )
                if graphql_backoff_seconds > 7200:
                    raise ValueError("mediamarkt: GraphQL backoff seconds must be between 1 and 7200.")

                graphql_backoff_multiplier = self._parse_float_value(
                    site_payload.get(
                        "mediamarkt_graphql_backoff_multiplier",
                        current_cfg.mediamarkt_graphql_backoff_multiplier(),
                    ),
                    current_cfg.mediamarkt_graphql_backoff_multiplier(),
                    minimum=1.0,
                )
                if graphql_backoff_multiplier > 20:
                    raise ValueError("mediamarkt: GraphQL backoff multiplier must be between 1 and 20.")

                graphql_max_backoff_seconds = self._parse_float_value(
                    site_payload.get(
                        "mediamarkt_graphql_max_backoff_seconds",
                        current_cfg.mediamarkt_graphql_max_backoff_seconds(),
                    ),
                    current_cfg.mediamarkt_graphql_max_backoff_seconds(),
                    minimum=1.0,
                )
                if graphql_max_backoff_seconds < graphql_backoff_seconds or graphql_max_backoff_seconds > 7200:
                    raise ValueError(
                        "mediamarkt: GraphQL max backoff seconds must be at least the base backoff and no more than 7200."
                    )

                site_entry.update(
                    {
                        "mediamarkt_graphql_backoff_seconds": graphql_backoff_seconds,
                        "mediamarkt_graphql_backoff_multiplier": graphql_backoff_multiplier,
                        "mediamarkt_graphql_max_backoff_seconds": graphql_max_backoff_seconds,
                    }
                )
            file_payload["sites"][site] = site_entry

        backoff_multiplier = self._parse_float_value(
            watchlist_payload.get("backoff_multiplier", current_cfg.watchlist_backoff_multiplier()),
            current_cfg.watchlist_backoff_multiplier(),
            minimum=1.0,
        )
        if backoff_multiplier > 20:
            raise ValueError("watchlist backoff_multiplier must be between 1 and 20.")

        watchlist_site_entries: dict[str, dict[str, Any]] = {}
        max_watchlist_interval = 1.0
        for site in SITE_LABELS:
            site_payload = (
                watchlist_site_payloads.get(site, {})
                if isinstance(watchlist_site_payloads.get(site), dict)
                else {}
            )
            interval_seconds = self._parse_float_value(
                site_payload.get("interval_seconds", current_cfg.watchlist_interval_seconds(site)),
                current_cfg.watchlist_interval_seconds(site),
                minimum=1.0,
            )
            if interval_seconds < 1 or interval_seconds > 3600:
                raise ValueError(f"{site}: watchlist interval_seconds must be between 1 and 3600.")

            max_concurrency = self._parse_int_value(
                site_payload.get("max_concurrency", current_cfg.watchlist_max_concurrency(site)),
                current_cfg.watchlist_max_concurrency(site),
                minimum=1,
            )
            if max_concurrency < 1 or max_concurrency > 20:
                raise ValueError(f"{site}: watchlist max_concurrency must be between 1 and 20.")

            request_timeout_seconds = self._parse_float_value(
                site_payload.get("request_timeout_seconds", current_cfg.watchlist_request_timeout_seconds(site)),
                current_cfg.watchlist_request_timeout_seconds(site),
                minimum=1.0,
            )
            if request_timeout_seconds < 1 or request_timeout_seconds > 120:
                raise ValueError(f"{site}: watchlist request_timeout_seconds must be between 1 and 120.")

            jitter_seconds = self._parse_float_value(
                site_payload.get("jitter_seconds", current_cfg.watchlist_jitter_seconds(site)),
                current_cfg.watchlist_jitter_seconds(site),
                minimum=0.0,
            )
            if jitter_seconds < 0 or jitter_seconds > 60:
                raise ValueError(f"{site}: watchlist jitter_seconds must be between 0 and 60.")

            max_watchlist_interval = max(max_watchlist_interval, interval_seconds)
            watchlist_site_entries[site] = {
                "enabled": bool(site_payload.get("enabled", current_cfg.watchlist_site_enabled(site))),
                "interval_seconds": interval_seconds,
                "max_concurrency": max_concurrency,
                "request_timeout_seconds": request_timeout_seconds,
                "jitter_seconds": jitter_seconds,
            }

        max_backoff_seconds = self._parse_float_value(
            watchlist_payload.get("max_backoff_seconds", current_cfg.watchlist_max_backoff_seconds()),
            current_cfg.watchlist_max_backoff_seconds(),
            minimum=1.0,
        )
        if max_backoff_seconds < max_watchlist_interval:
            raise ValueError("watchlist max_backoff_seconds must be greater than or equal to every watchlist interval_seconds.")
        if max_backoff_seconds > 7200:
            raise ValueError("watchlist max_backoff_seconds must be 7200 seconds or less.")

        file_payload["watchlist"] = {
            "enabled": bool(watchlist_payload.get("enabled", current_cfg.watchlist_enabled())),
            "backoff_on_429": bool(watchlist_payload.get("backoff_on_429", current_cfg.watchlist_backoff_on_429())),
            "backoff_multiplier": backoff_multiplier,
            "max_backoff_seconds": max_backoff_seconds,
            "pause_site_on_error": bool(
                watchlist_payload.get("pause_site_on_error", current_cfg.watchlist_pause_site_on_error())
            ),
            "sites": watchlist_site_entries,
        }

        self._write_env_updates(env_updates)
        self._write_scan_settings_map(file_payload)
        return self.get_scan_settings_effective()

    def reset_scan_settings_defaults(self) -> dict[str, Any]:
        self._write_scan_settings_map(
            {
                "global": {
                    "request_timeout_seconds": DEFAULT_SCAN_GLOBALS["request_timeout_seconds"],
                    "retry_delay_seconds": DEFAULT_SCAN_GLOBALS["retry_delay_seconds"],
                    "max_retries": DEFAULT_SCAN_GLOBALS["max_retries"],
                },
                "sites": {
                    site: {
                        "cooldown_seconds": defaults["cooldown_seconds"],
                        "request_timeout_seconds": defaults["request_timeout_seconds"],
                        "page_delay_seconds": defaults["page_delay_seconds"] or 0.0,
                        **({"max_pages": defaults["max_pages"]} if defaults.get("max_pages") is not None else {}),
                        **(
                            {
                                "mediamarkt_graphql_backoff_seconds": defaults[
                                    "mediamarkt_graphql_backoff_seconds"
                                ],
                                "mediamarkt_graphql_backoff_multiplier": defaults[
                                    "mediamarkt_graphql_backoff_multiplier"
                                ],
                                "mediamarkt_graphql_max_backoff_seconds": defaults[
                                    "mediamarkt_graphql_max_backoff_seconds"
                                ],
                            }
                            if site == "mediamarkt"
                            else {}
                        ),
                    }
                    for site, defaults in SITE_SCAN_DEFAULTS.items()
                },
                "watchlist": WATCHLIST_DEFAULTS,
            }
        )
        self._write_env_updates({"PARSER_CONCURRENCY": int(DEFAULT_SCAN_GLOBALS["max_parallel_parsers"])})
        return self.get_scan_settings_effective()

    def create_chrome_profile(self) -> dict[str, Any]:
        cfg = self.load_app_config()
        binary_candidates = [
            cfg.chrome_binary,
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        chrome_binary = next((path for path in binary_candidates if path and Path(path).exists()), "")
        if not chrome_binary:
            raise ValueError("Chrome binary was not found. Set CHROME_BINARY or install Google Chrome.")

        user_data_dir = cfg.chrome_user_data_dir or str(self.base_dir.parent / "chrome-profile")
        profile_dir = cfg.chrome_profile_dir or "Default"
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)

        creation_flags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creation_flags |= subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creation_flags |= subprocess.CREATE_NEW_PROCESS_GROUP

        logger.info(
            "chrome_profile_bootstrap_requested",
            extra={
                "function": "ConfigManager.create_chrome_profile",
                "caller_stack": [line.strip() for line in traceback.format_stack(limit=8)],
                "chrome_binary": chrome_binary,
                "chrome_user_data_dir": user_data_dir,
                "chrome_profile_dir": profile_dir,
                "runtime_running": False,
            },
        )
        logger.info(
            "chrome_process_spawn_requested",
            extra={
                "function": "ConfigManager.create_chrome_profile",
                "caller_stack": [line.strip() for line in traceback.format_stack(limit=8)],
                "chrome_binary": chrome_binary,
                "chrome_user_data_dir": user_data_dir,
                "chrome_profile_dir": profile_dir,
                "runtime_running": False,
            },
        )
        process = subprocess.Popen(
            [
                chrome_binary,
                f"--user-data-dir={user_data_dir}",
                f"--profile-directory={profile_dir}",
                cfg.selenium_test_url or "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
            close_fds=True,
        )
        logger.info(
            "chrome_process_spawned",
            extra={
                "function": "ConfigManager.create_chrome_profile",
                "pid": process.pid,
                "chrome_binary": chrome_binary,
                "chrome_user_data_dir": user_data_dir,
                "chrome_profile_dir": profile_dir,
            },
        )
        logger.info(
            "chrome_process_detected",
            extra={
                "function": "ConfigManager.create_chrome_profile",
                "pid": process.pid,
                "chrome_binary": chrome_binary,
                "chrome_user_data_dir": user_data_dir,
                "chrome_profile_dir": profile_dir,
            },
        )

        return {
            "ok": True,
            "message": "Chrome profile launched in a separate process.",
            "pid": process.pid,
            "chrome_binary": chrome_binary,
            "chrome_user_data_dir": user_data_dir,
            "chrome_profile_dir": profile_dir,
        }
