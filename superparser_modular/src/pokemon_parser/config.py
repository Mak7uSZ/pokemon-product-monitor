from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from pokemon_parser.utils.time import is_turbo_time, load_timezone

SCAN_SETTINGS_FILENAME = "scan_settings.json"
DEFAULT_SCAN_GLOBALS = {
    "scan_interval_seconds": 60.0,
    "request_timeout_seconds": 20.0,
    "retry_delay_seconds": 0.35,
    "max_retries": 2,
    "max_parallel_parsers": 4,
}
WORKER_SPEED_PROFILES = {"safe", "balanced", "fast", "custom"}
SITE_SCAN_DEFAULTS = {
    "mediamarkt": {
        "cooldown_seconds": 45.0,
        "request_timeout_seconds": 20.0,
        "max_pages": 20,
        "page_delay_seconds": 0.4,
        "mediamarkt_graphql_backoff_seconds": 45.0,
        "mediamarkt_graphql_backoff_multiplier": 2.0,
        "mediamarkt_graphql_max_backoff_seconds": 1800.0,
        "mediamarkt_graphql_soft_deny_escalation_threshold": 3,
        "mediamarkt_graphql_soft_deny_window_seconds": 300.0,
    },
    "dreamland": {
        "cooldown_seconds": 45.0,
        "request_timeout_seconds": 20.0,
        "max_pages": 25,
        "page_delay_seconds": 0.25,
    },
    "bol": {
        "cooldown_seconds": 30.0,
        "request_timeout_seconds": 20.0,
        "max_pages": 20,
        "page_delay_seconds": 0.0,
    },
    "pocketgames": {
        "cooldown_seconds": 30.0,
        "request_timeout_seconds": 10.0,
        "max_pages": None,
        "page_delay_seconds": 0.0,
    },
}

WATCHLIST_DEFAULTS = {
    "enabled": True,
    "request_timeout_seconds": 12.0,
    "jitter_seconds": 0.5,
    "backoff_on_429": True,
    "backoff_multiplier": 2.0,
    "max_backoff_seconds": 300.0,
    "pause_site_on_error": False,
    "sites": {
        "mediamarkt": {
            "enabled": True,
            "interval_seconds": 8.0,
            "max_concurrency": 1,
            "request_timeout_seconds": 12.0,
            "jitter_seconds": 0.5,
        },
        "dreamland": {
            "enabled": True,
            "interval_seconds": 12.0,
            "max_concurrency": 2,
            "request_timeout_seconds": 12.0,
            "jitter_seconds": 0.5,
        },
        "bol": {
            "enabled": True,
            "interval_seconds": 12.0,
            "max_concurrency": 1,
            "request_timeout_seconds": 12.0,
            "jitter_seconds": 0.5,
        },
        "pocketgames": {
            "enabled": True,
            "interval_seconds": 8.0,
            "max_concurrency": 2,
            "request_timeout_seconds": 12.0,
            "jitter_seconds": 0.5,
        },
    },
}


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _float_env(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw in {None, ""}:
        value = default
    else:
        try:
            value = float(str(raw).strip())
        except Exception:
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw in {None, ""}:
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except Exception:
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _load_scan_settings_file(base_dir: Path) -> dict[str, Any]:
    path = base_dir / SCAN_SETTINGS_FILENAME
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    env_path: Path
    filters_json_path: Path
    db_file: str
    timezone_name: str
    tz: object
    action_mode: str
    telegram_bot_token: str
    telegram_chat_id: str
    turbo_start_hour: int
    turbo_start_minute: int
    bol_turbo_interval: float
    bol_low_interval: float
    pocket_turbo_interval: float
    pocket_low_interval: float
    jitter: float
    parser_concurrency: int
    heartbeat_interval_seconds: int
    snapshot_mode: str
    enable_notifications: bool
    enable_heartbeat_alerts: bool
    enable_success_alerts: bool
    enable_error_alerts: bool
    enable_bol: bool
    enable_pocketgames: bool
    enable_mediamarkt: bool
    enable_dreamland: bool
    proxy_enabled: bool
    proxy_type: str
    proxy_host: str
    proxy_port: int
    proxy_login: str
    proxy_password: str
    timer_enabled: bool
    timer_interval: int
    timer_unit: str
    chrome_binary: str
    chrome_user_data_dir: str
    chrome_profile_dir: str
    selenium_test_url: str
    selenium_prewarm: bool
    selenium_prewarm_enabled: bool
    selenium_prewarm_on_runtime_start: bool
    selenium_keep_browser_alive: bool
    watchlist_warm_tabs_enabled: bool
    watchlist_warm_tabs_max: int
    watchlist_warm_tab_refresh_interval_seconds: float
    watchlist_warm_tab_min_refresh_interval_seconds: float
    watchlist_warm_tab_reload_timeout_seconds: float
    watchlist_warm_tab_stale_after_seconds: float
    challenge_cooldown_base_seconds: float
    challenge_cooldown_multiplier: float
    challenge_cooldown_max_seconds: float
    challenge_cooldown_jitter_ratio: float
    mediamarkt_warm_tabs_enabled: bool
    mediamarkt_fast_action_refresh_policy: str
    mediamarkt_warm_recent_threshold_seconds: float
    bol_buy_now_url: str
    scan_settings: dict[str, Any]
    queue_check_enabled: bool
    queue_wait_timeout_seconds: float
    queue_poll_seconds: float
    worker_speed_profile: str
    worker_click_pause_seconds: float
    worker_after_navigation_wait_seconds: float
    worker_after_add_to_cart_wait_seconds: float
    worker_after_checkout_click_wait_seconds: float
    worker_wait_timeout_seconds: float
    worker_poll_seconds: float
    worker_retry_pause_seconds: float
    worker_telegram_trace_enabled: bool
    worker_telegram_trace_level: str
    worker_trace_queue_update_seconds: float
    worker_low_level_debug: bool
    verbose_filters: bool = False
    verbose_selenium: bool = False
    allow_legacy_backup_restore: bool = False

    checkout_email: str = ""
    checkout_first_name: str = ""
    checkout_last_name: str = ""
    checkout_street: str = ""
    checkout_house_number: str = ""
    checkout_zip_code: str = ""
    checkout_city: str = ""

    checkout_card_number: str = ""
    checkout_card_expiry: str = ""
    checkout_card_cvv: str = ""
    checkout_card_name: str = ""

    def parser_enabled_map(self) -> dict[str, bool]:
        return {
            "mediamarkt": self.enable_mediamarkt,
            "dreamland": self.enable_dreamland,
            "bol": self.enable_bol,
            "pocketgames": self.enable_pocketgames,
        }

    def is_parser_enabled(self, site: str) -> bool:
        return bool(self.parser_enabled_map().get(site, False))

    def enabled_parser_sites(self) -> tuple[str, ...]:
        return tuple(
            site
            for site, enabled in self.parser_enabled_map().items()
            if enabled
        )

    def resolved_db_path(self) -> Path:
        path = Path(self.db_file)
        return path if path.is_absolute() else self.base_dir / path

    def scan_settings_path(self) -> Path:
        return self.base_dir / SCAN_SETTINGS_FILENAME

    def proxy_url(self) -> str | None:
        if not self.proxy_enabled or not self.proxy_host or self.proxy_port <= 0:
            return None
        scheme = self.proxy_type if self.proxy_type in {"http", "https", "socks5"} else "http"
        return f"{scheme}://{self.proxy_host}:{self.proxy_port}"

    def _scan_global(self) -> dict[str, Any]:
        payload = self.scan_settings.get("global", {}) if isinstance(self.scan_settings, dict) else {}
        return payload if isinstance(payload, dict) else {}

    def _scan_site(self, site: str) -> dict[str, Any]:
        sites = self.scan_settings.get("sites", {}) if isinstance(self.scan_settings, dict) else {}
        if not isinstance(sites, dict):
            return {}
        payload = sites.get(site, {})
        return payload if isinstance(payload, dict) else {}

    def _watchlist_settings(self) -> dict[str, Any]:
        payload = self.scan_settings.get("watchlist", {}) if isinstance(self.scan_settings, dict) else {}
        return payload if isinstance(payload, dict) else {}

    def _watchlist_site(self, site: str) -> dict[str, Any]:
        payload = self._watchlist_settings().get("sites", {})
        if not isinstance(payload, dict):
            return {}
        site_payload = payload.get(site, {})
        return site_payload if isinstance(site_payload, dict) else {}

    def scan_interval_override_seconds(self) -> float | None:
        value = _float_or_none(self._scan_global().get("scan_interval_seconds"))
        return max(0.1, value) if value is not None else None

    def request_timeout_override_seconds(self) -> float | None:
        value = _float_or_none(self._scan_global().get("request_timeout_seconds"))
        return max(1.0, value) if value is not None else None

    def retry_delay_seconds(self) -> float:
        value = _float_or_none(self._scan_global().get("retry_delay_seconds"))
        return max(0.0, value) if value is not None else float(DEFAULT_SCAN_GLOBALS["retry_delay_seconds"])

    def max_retries(self) -> int:
        value = _int_or_none(self._scan_global().get("max_retries"))
        return max(0, value) if value is not None else int(DEFAULT_SCAN_GLOBALS["max_retries"])

    def legacy_site_interval(self, site: str) -> float:
        turbo = is_turbo_time(self.tz, self.turbo_start_hour, self.turbo_start_minute)

        if site == "bol":
            base = self.bol_turbo_interval if turbo else self.bol_low_interval
        elif site == "pocketgames":
            base = self.pocket_turbo_interval if turbo else self.pocket_low_interval
        elif site == "mediamarkt":
            mediamarkt_turbo = getattr(self, "mediamarkt_turbo_interval", self.pocket_turbo_interval)
            mediamarkt_low = getattr(self, "mediamarkt_low_interval", self.pocket_low_interval)
            base = mediamarkt_turbo if turbo else mediamarkt_low
        elif site == "dreamland":
            dreamland_turbo = getattr(
                self,
                "dreamland_turbo_interval",
                getattr(self, "mediamarkt_turbo_interval", self.pocket_turbo_interval),
            )
            dreamland_low = getattr(
                self,
                "dreamland_low_interval",
                getattr(self, "mediamarkt_low_interval", self.pocket_low_interval),
            )
            base = dreamland_turbo if turbo else dreamland_low
        else:
            base = self.pocket_turbo_interval if turbo else self.pocket_low_interval

        return max(0.1, float(base))

    def effective_scan_delay_seconds(self, site: str) -> float:
        site_value = _float_or_none(self._scan_site(site).get("scan_delay_seconds"))
        if site_value is not None:
            return max(0.1, site_value)

        global_value = self.scan_interval_override_seconds()
        if global_value is not None:
            return max(0.1, global_value)

        return self.legacy_site_interval(site)

    def site_cooldown_seconds(self, site: str) -> float:
        site_value = _float_or_none(self._scan_site(site).get("cooldown_seconds"))
        if site_value is not None:
            return max(1.0, site_value)
        return float(SITE_SCAN_DEFAULTS.get(site, SITE_SCAN_DEFAULTS["pocketgames"])["cooldown_seconds"])

    def site_request_timeout_seconds(self, site: str) -> float:
        site_value = _float_or_none(self._scan_site(site).get("request_timeout_seconds"))
        if site_value is not None:
            return max(1.0, site_value)

        global_value = self.request_timeout_override_seconds()
        if global_value is not None:
            return max(1.0, global_value)

        return float(SITE_SCAN_DEFAULTS.get(site, SITE_SCAN_DEFAULTS["pocketgames"])["request_timeout_seconds"])

    def site_max_pages(self, site: str) -> int | None:
        site_value = _int_or_none(self._scan_site(site).get("max_pages"))
        if site_value is not None:
            return max(1, site_value)
        return SITE_SCAN_DEFAULTS.get(site, SITE_SCAN_DEFAULTS["pocketgames"])["max_pages"]

    def site_page_delay_seconds(self, site: str) -> float:
        site_value = _float_or_none(self._scan_site(site).get("page_delay_seconds"))
        if site_value is not None:
            return max(0.0, site_value)
        return float(SITE_SCAN_DEFAULTS.get(site, SITE_SCAN_DEFAULTS["pocketgames"])["page_delay_seconds"] or 0.0)

    def mediamarkt_graphql_backoff_seconds(self) -> float:
        value = _float_or_none(self._scan_site("mediamarkt").get("mediamarkt_graphql_backoff_seconds"))
        if value is not None:
            return max(1.0, value)
        return float(SITE_SCAN_DEFAULTS["mediamarkt"]["mediamarkt_graphql_backoff_seconds"])

    def mediamarkt_graphql_backoff_multiplier(self) -> float:
        value = _float_or_none(self._scan_site("mediamarkt").get("mediamarkt_graphql_backoff_multiplier"))
        if value is not None:
            return max(1.0, value)
        return float(SITE_SCAN_DEFAULTS["mediamarkt"]["mediamarkt_graphql_backoff_multiplier"])

    def mediamarkt_graphql_max_backoff_seconds(self) -> float:
        value = _float_or_none(self._scan_site("mediamarkt").get("mediamarkt_graphql_max_backoff_seconds"))
        if value is not None:
            return max(1.0, value)
        return float(SITE_SCAN_DEFAULTS["mediamarkt"]["mediamarkt_graphql_max_backoff_seconds"])

    def mediamarkt_graphql_soft_deny_escalation_threshold(self) -> int:
        value = _int_or_none(
            self._scan_site("mediamarkt").get("mediamarkt_graphql_soft_deny_escalation_threshold")
        )
        if value is not None:
            return max(1, value)
        return int(SITE_SCAN_DEFAULTS["mediamarkt"]["mediamarkt_graphql_soft_deny_escalation_threshold"])

    def mediamarkt_graphql_soft_deny_window_seconds(self) -> float:
        value = _float_or_none(
            self._scan_site("mediamarkt").get("mediamarkt_graphql_soft_deny_window_seconds")
        )
        if value is not None:
            return max(1.0, value)
        return float(SITE_SCAN_DEFAULTS["mediamarkt"]["mediamarkt_graphql_soft_deny_window_seconds"])

    def watchlist_enabled(self) -> bool:
        settings = self._watchlist_settings()
        if "enabled" in settings:
            return bool(settings.get("enabled"))
        return bool(WATCHLIST_DEFAULTS["enabled"])

    def watchlist_site_enabled(self, site: str) -> bool:
        site_settings = self._watchlist_site(site)
        if "enabled" in site_settings:
            return bool(site_settings.get("enabled"))
        defaults = WATCHLIST_DEFAULTS["sites"].get(site, WATCHLIST_DEFAULTS["sites"]["pocketgames"])
        return bool(defaults.get("enabled", True))

    def watchlist_interval_seconds(self, site: str) -> float:
        value = _float_or_none(self._watchlist_site(site).get("interval_seconds"))
        if value is None:
            value = _float_or_none(self._watchlist_site(site).get("watchlist_interval_seconds"))
        if value is not None:
            return max(1.0, value)
        defaults = WATCHLIST_DEFAULTS["sites"].get(site, WATCHLIST_DEFAULTS["sites"]["pocketgames"])
        return float(defaults["interval_seconds"])

    def watchlist_max_concurrency(self, site: str) -> int:
        value = _int_or_none(self._watchlist_site(site).get("max_concurrency"))
        if value is None:
            value = _int_or_none(self._watchlist_site(site).get("watchlist_max_concurrency"))
        if value is not None:
            return max(1, value)
        defaults = WATCHLIST_DEFAULTS["sites"].get(site, WATCHLIST_DEFAULTS["sites"]["pocketgames"])
        return int(defaults["max_concurrency"])

    def watchlist_request_timeout_seconds(self, site: str | None = None) -> float:
        if site:
            site_value = _float_or_none(self._watchlist_site(site).get("request_timeout_seconds"))
            if site_value is not None:
                return max(1.0, site_value)
        value = _float_or_none(self._watchlist_settings().get("request_timeout_seconds"))
        return max(1.0, value) if value is not None else float(WATCHLIST_DEFAULTS["request_timeout_seconds"])

    def watchlist_jitter_seconds(self, site: str | None = None) -> float:
        if site:
            site_value = _float_or_none(self._watchlist_site(site).get("jitter_seconds"))
            if site_value is not None:
                return max(0.0, site_value)
        value = _float_or_none(self._watchlist_settings().get("jitter_seconds"))
        return max(0.0, value) if value is not None else float(WATCHLIST_DEFAULTS["jitter_seconds"])

    def watchlist_backoff_on_429(self) -> bool:
        settings = self._watchlist_settings()
        if "backoff_on_429" in settings:
            return bool(settings.get("backoff_on_429"))
        return bool(WATCHLIST_DEFAULTS["backoff_on_429"])

    def watchlist_backoff_multiplier(self) -> float:
        value = _float_or_none(self._watchlist_settings().get("backoff_multiplier"))
        return max(1.0, value) if value is not None else float(WATCHLIST_DEFAULTS["backoff_multiplier"])

    def watchlist_max_backoff_seconds(self) -> float:
        value = _float_or_none(self._watchlist_settings().get("max_backoff_seconds"))
        return max(1.0, value) if value is not None else float(WATCHLIST_DEFAULTS["max_backoff_seconds"])

    def watchlist_pause_site_on_error(self) -> bool:
        settings = self._watchlist_settings()
        if "pause_site_on_error" in settings:
            return bool(settings.get("pause_site_on_error"))
        return bool(WATCHLIST_DEFAULTS["pause_site_on_error"])

    def watchlist_warm_tabs_allowed(self) -> bool:
        return bool(self.watchlist_warm_tabs_enabled and self.selenium_keep_browser_alive)

    def selenium_runtime_config(self) -> dict[str, Any]:
        return {
            "action_mode": self.action_mode,
            "legacy_prewarm": bool(self.selenium_prewarm),
            "prewarm_enabled": bool(self.selenium_prewarm_enabled),
            "prewarm_on_runtime_start": bool(self.selenium_prewarm_on_runtime_start),
            "keep_browser_alive": bool(self.selenium_keep_browser_alive),
            "warm_tabs_enabled": bool(self.watchlist_warm_tabs_enabled),
            "warm_tabs_max": int(self.watchlist_warm_tabs_max),
            "challenge_cooldown_base_seconds": float(self.challenge_cooldown_base_seconds),
            "challenge_cooldown_multiplier": float(self.challenge_cooldown_multiplier),
            "challenge_cooldown_max_seconds": float(self.challenge_cooldown_max_seconds),
            "challenge_cooldown_jitter_ratio": float(self.challenge_cooldown_jitter_ratio),
            "mediamarkt_warm_tabs_enabled": bool(self.mediamarkt_warm_tabs_enabled),
            "mediamarkt_fast_action_refresh_policy": self.mediamarkt_fast_action_refresh_policy,
            "mediamarkt_warm_recent_threshold_seconds": float(self.mediamarkt_warm_recent_threshold_seconds),
        }

    def selenium_prewarm_skip_reason(self) -> str | None:
        if self.action_mode != "selenium":
            return "action_mode_not_selenium"
        if not self.selenium_prewarm_enabled:
            return "prewarm_disabled"
        if not self.selenium_prewarm_on_runtime_start:
            return "prewarm_on_runtime_start_disabled"
        return None

    def should_prewarm_selenium_on_runtime_start(self) -> bool:
        return self.selenium_prewarm_skip_reason() is None

    def effective_parallel_parsers(self) -> int:
        enabled_count = max(1, len(self.enabled_parser_sites()))
        return max(1, min(self.parser_concurrency, enabled_count))

    def timer_interval_seconds(self) -> int:
        unit = self.timer_unit.lower().strip()
        interval = max(1, int(self.timer_interval))
        if unit == "minutes":
            return interval * 60
        if unit == "hours":
            return interval * 3600
        return interval


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_first_non_empty(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return None


def load_config(
    base_dir: Path | None = None,
    verbose_filters: bool = False,
    verbose_selenium: bool = False,
) -> AppConfig:
    current_base = (base_dir or Path.cwd()).resolve()
    env_path = current_base / ".env"
    filters_path = current_base / "filters.json"
    scan_settings = _load_scan_settings_file(current_base)

    if env_path.exists():
        load_dotenv(dotenv_path=str(env_path), override=True, encoding="utf-8")

    timezone_name = os.environ.get("TIMEZONE", "Europe/Amsterdam").strip()
    action_mode = os.environ.get("ACTION_MODE", "off").strip().lower()
    if action_mode == "telegram":
        action_mode = "notify_only"
    if action_mode not in {"off", "notify_only", "selenium"}:
        action_mode = "notify_only"

    parser_concurrency = max(1, int(os.environ.get("PARSER_CONCURRENCY", "4")))
    heartbeat_interval_seconds = max(60, int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "3600")))
    proxy_type = os.environ.get("PROXY_TYPE", "http").strip().lower()
    if proxy_type not in {"http", "https", "socks5"}:
        proxy_type = "http"
    timer_unit = os.environ.get("TIMER_UNIT", "minutes").strip().lower()
    if timer_unit not in {"seconds", "minutes", "hours"}:
        timer_unit = "minutes"
    worker_speed_profile = os.environ.get("WORKER_SPEED_PROFILE", "balanced").strip().lower()
    if worker_speed_profile not in WORKER_SPEED_PROFILES:
        worker_speed_profile = "balanced"
    worker_trace_level = os.environ.get("WORKER_TELEGRAM_TRACE_LEVEL", "normal").strip().lower()
    if worker_trace_level not in {"minimal", "normal", "verbose"}:
        worker_trace_level = "normal"
    mediamarkt_refresh_policy = os.environ.get(
        "MEDIAMARKT_FAST_ACTION_REFRESH_POLICY",
        os.environ.get("mediamarkt_fast_action_refresh_policy", "never_if_warm_recent"),
    ).strip().lower()
    if mediamarkt_refresh_policy not in {"never_if_warm_recent", "micro_revalidate_only", "always_refresh"}:
        mediamarkt_refresh_policy = "never_if_warm_recent"

    return AppConfig(
        base_dir=current_base,
        env_path=env_path,
        filters_json_path=filters_path,
        db_file=os.environ.get("DB_FILE", "multi_site_monitor.db").strip(),
        timezone_name=timezone_name,
        tz=load_timezone(timezone_name),
        action_mode=action_mode,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        turbo_start_hour=int(os.environ.get("TURBO_START_HOUR", "11")),
        turbo_start_minute=int(os.environ.get("TURBO_START_MINUTE", "50")),
        bol_turbo_interval=float(os.environ.get("BOL_TURBO_INTERVAL", "2.0")),
        bol_low_interval=float(os.environ.get("BOL_LOW_INTERVAL", "12.0")),
        pocket_turbo_interval=float(os.environ.get("POCKET_TURBO_INTERVAL", "0.5")),
        pocket_low_interval=float(os.environ.get("POCKET_LOW_INTERVAL", "4.0")),
        jitter=float(os.environ.get("JITTER", "0.15")),
        parser_concurrency=parser_concurrency,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        snapshot_mode=os.environ.get("SNAPSHOT_MODE", "changes").strip().lower(),
        enable_notifications=parse_bool(os.environ.get("ENABLE_NOTIFICATIONS"), False),
        enable_heartbeat_alerts=parse_bool(os.environ.get("ENABLE_HEARTBEAT_ALERTS"), True),
        enable_success_alerts=parse_bool(os.environ.get("ENABLE_SUCCESS_ALERTS"), True),
        enable_error_alerts=parse_bool(os.environ.get("ENABLE_ERROR_ALERTS"), True),
        enable_bol=parse_bool(os.environ.get("ENABLE_BOL"), False),
        enable_pocketgames=parse_bool(
            os.environ.get("ENABLE_POCKETGAMES", os.environ.get("ENABLE_POCKET_GAMES")),
            False,
        ),
        enable_mediamarkt=parse_bool(os.environ.get("ENABLE_MEDIAMARKT"), False),
        enable_dreamland=parse_bool(os.environ.get("ENABLE_DREAMLAND"), False),
        proxy_enabled=parse_bool(os.environ.get("PROXY_ENABLED"), False),
        proxy_type=proxy_type,
        proxy_host=os.environ.get("PROXY_HOST", "").strip(),
        proxy_port=max(0, int(os.environ.get("PROXY_PORT", "0") or "0")),
        proxy_login=os.environ.get("PROXY_LOGIN", "").strip(),
        proxy_password=os.environ.get("PROXY_PASSWORD", "").strip(),
        timer_enabled=parse_bool(os.environ.get("ENABLE_TIMER"), False),
        timer_interval=max(1, int(os.environ.get("TIMER_INTERVAL", "15") or "15")),
        timer_unit=timer_unit,
        chrome_binary=os.environ.get("CHROME_BINARY", "").strip(),
        chrome_user_data_dir=os.environ.get("CHROME_USER_DATA_DIR", "").strip(),
        chrome_profile_dir=os.environ.get("CHROME_PROFILE_DIR", "").strip(),
        selenium_test_url=os.environ.get("SELENIUM_TEST_URL", "https://www.bol.com/nl/nl/").strip(),
        selenium_prewarm=parse_bool(os.environ.get("SELENIUM_PREWARM"), False),
        selenium_prewarm_enabled=parse_bool(
            _env_first_non_empty("SELENIUM_PREWARM_ENABLED", "SELENIUM_PREWARM"),
            False,
        ),
        selenium_prewarm_on_runtime_start=parse_bool(
            _env_first_non_empty("SELENIUM_PREWARM_ON_RUNTIME_START", "SELENIUM_PREWARM"),
            False,
        ),
        selenium_keep_browser_alive=parse_bool(
            _env_first_non_empty("SELENIUM_KEEP_BROWSER_ALIVE", "SELENIUM_PREWARM"),
            False,
        ),
        watchlist_warm_tabs_enabled=parse_bool(os.environ.get("WATCHLIST_WARM_TABS_ENABLED"), False),
        watchlist_warm_tabs_max=max(1, int(os.environ.get("WATCHLIST_WARM_TABS_MAX", "6") or "6")),
        watchlist_warm_tab_refresh_interval_seconds=_float_env(
            "WATCHLIST_WARM_TAB_REFRESH_INTERVAL_SECONDS",
            30.0,
            minimum=1.0,
        ),
        watchlist_warm_tab_min_refresh_interval_seconds=_float_env(
            "WATCHLIST_WARM_TAB_MIN_REFRESH_INTERVAL_SECONDS",
            15.0,
            minimum=1.0,
        ),
        watchlist_warm_tab_reload_timeout_seconds=_float_env(
            "WATCHLIST_WARM_TAB_RELOAD_TIMEOUT_SECONDS",
            8.0,
            minimum=1.0,
        ),
        watchlist_warm_tab_stale_after_seconds=_float_env(
            "WATCHLIST_WARM_TAB_STALE_AFTER_SECONDS",
            60.0,
            minimum=1.0,
        ),
        challenge_cooldown_base_seconds=_float_env(
            "CHALLENGE_COOLDOWN_BASE_SECONDS",
            30.0,
            minimum=1.0,
        ),
        challenge_cooldown_multiplier=_float_env(
            "CHALLENGE_COOLDOWN_MULTIPLIER",
            2.0,
            minimum=1.0,
        ),
        challenge_cooldown_max_seconds=_float_env(
            "CHALLENGE_COOLDOWN_MAX_SECONDS",
            900.0,
            minimum=1.0,
        ),
        challenge_cooldown_jitter_ratio=_float_env(
            "CHALLENGE_COOLDOWN_JITTER_RATIO",
            0.1,
            minimum=0.0,
        ),
        mediamarkt_warm_tabs_enabled=parse_bool(os.environ.get("MEDIAMARKT_WARM_TABS_ENABLED"), False),
        mediamarkt_fast_action_refresh_policy=mediamarkt_refresh_policy,
        mediamarkt_warm_recent_threshold_seconds=_float_env(
            "MEDIAMARKT_WARM_RECENT_THRESHOLD_SECONDS",
            60.0,
            minimum=0.0,
        ),
        bol_buy_now_url=os.environ.get("BOL_BUY_NOW_URL", "https://www.bol.com/nl/nl/checkout/?entryPoint=BUY_NOW").strip(),
        scan_settings=scan_settings,
        queue_check_enabled=parse_bool(os.environ.get("QUEUE_CHECK_ENABLED"), True),
        queue_wait_timeout_seconds=_float_env("QUEUE_WAIT_TIMEOUT_SECONDS", 300.0, minimum=1.0),
        queue_poll_seconds=_float_env("QUEUE_POLL_SECONDS", 1.0, minimum=0.1),
        worker_speed_profile=worker_speed_profile,
        worker_click_pause_seconds=_float_env("WORKER_CLICK_PAUSE_SECONDS", 0.2, minimum=0.0),
        worker_after_navigation_wait_seconds=_float_env("WORKER_AFTER_NAVIGATION_WAIT_SECONDS", 0.5, minimum=0.0),
        worker_after_add_to_cart_wait_seconds=_float_env("WORKER_AFTER_ADD_TO_CART_WAIT_SECONDS", 0.6, minimum=0.0),
        worker_after_checkout_click_wait_seconds=_float_env("WORKER_AFTER_CHECKOUT_CLICK_WAIT_SECONDS", 0.6, minimum=0.0),
        worker_wait_timeout_seconds=_float_env("WORKER_WAIT_TIMEOUT_SECONDS", 20.0, minimum=1.0),
        worker_poll_seconds=_float_env("WORKER_POLL_SECONDS", 0.2, minimum=0.05),
        worker_retry_pause_seconds=_float_env("WORKER_RETRY_PAUSE_SECONDS", 0.45, minimum=0.0),
        worker_telegram_trace_enabled=parse_bool(os.environ.get("WORKER_TELEGRAM_TRACE_ENABLED"), False),
        worker_telegram_trace_level=worker_trace_level,
        worker_trace_queue_update_seconds=_float_env("WORKER_TRACE_QUEUE_UPDATE_SECONDS", 60.0, minimum=5.0),
        worker_low_level_debug=parse_bool(os.environ.get("WORKER_LOW_LEVEL_DEBUG"), False),
        verbose_filters=verbose_filters,
        verbose_selenium=verbose_selenium,
        allow_legacy_backup_restore=parse_bool(
            os.environ.get("ALLOW_LEGACY_BACKUP_RESTORE"),
            False,
        ),

        checkout_email=os.environ.get("CHECKOUT_EMAIL", "").strip(),
        checkout_first_name=os.environ.get("CHECKOUT_FIRST_NAME", "").strip(),
        checkout_last_name=os.environ.get("CHECKOUT_LAST_NAME", "").strip(),
        checkout_street=os.environ.get("CHECKOUT_STREET", "").strip(),
        checkout_house_number=os.environ.get("CHECKOUT_HOUSE_NUMBER", "").strip(),
        checkout_zip_code=os.environ.get("CHECKOUT_ZIP_CODE", "").strip(),
        checkout_city=os.environ.get("CHECKOUT_CITY", "").strip(),

        checkout_card_number=os.environ.get("CHECKOUT_CARD_NUMBER", "").strip(),
        checkout_card_expiry=os.environ.get("CHECKOUT_CARD_EXPIRY", "").strip(),
        checkout_card_cvv=os.environ.get("CHECKOUT_CARD_CVV", "").strip(),
        checkout_card_name=os.environ.get("CHECKOUT_CARD_NAME", "").strip(),
    )
