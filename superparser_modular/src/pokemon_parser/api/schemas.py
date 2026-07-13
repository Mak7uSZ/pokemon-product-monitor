from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CredentialsSaveRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class FilterPayload(BaseModel):
    name: str = ""
    sites: list[str] = Field(default_factory=list)
    keyword_groups: list[list[str]] = Field(default_factory=list)
    exclude_words: list[str] = Field(default_factory=list)
    min_price: float | None = None
    max_price: float | None = None
    soft_price: bool = True
    enabled: bool = True


class ProxyUpdateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    proxy_type: str = Field(default="http", alias="type")
    host: str = ""
    port: int = 0
    login: str = ""
    password: str = ""


class TelegramUpdateRequest(BaseModel):
    bot_token: str = ""
    chat_id: str = ""


class TelegramTestRequest(BaseModel):
    text: str | None = None


class NotificationsUpdateRequest(BaseModel):
    enabled: bool = True
    heartbeat_alerts: bool = True
    success_alerts: bool = True
    error_alerts: bool = True
    worker_trace_enabled: bool = True
    worker_trace_level: str = "normal"
    worker_trace_queue_update_seconds: float = 60.0


class TimerUpdateRequest(BaseModel):
    enabled: bool = False
    interval: int = 15
    unit: str = "minutes"


class ActionModeUpdateRequest(BaseModel):
    mode: str = "notify_only"


class WorkerSettingsUpdateRequest(BaseModel):
    queue_check_enabled: bool = True
    queue_wait_timeout_seconds: float = 300.0
    queue_poll_seconds: float = 1.0
    worker_speed_profile: str = "balanced"
    worker_click_pause_seconds: float = 0.2
    worker_after_navigation_wait_seconds: float = 0.5
    worker_after_add_to_cart_wait_seconds: float = 0.6
    worker_after_checkout_click_wait_seconds: float = 0.6
    worker_wait_timeout_seconds: float = 20.0
    worker_poll_seconds: float = 0.2
    worker_retry_pause_seconds: float = 0.45


class DbCleanupRequest(BaseModel):
    days: int = 30
    site: str | None = None


class SiteScanSettingsPayload(BaseModel):
    enabled: bool = True
    scan_delay_seconds: float = 1.0
    cooldown_seconds: float = 30.0
    request_timeout_seconds: float = 20.0
    max_pages: int | None = None
    page_delay_seconds: float | None = None


class GlobalScanSettingsPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scan_interval_seconds: float | None = None
    max_parallel_parsers: int = 4
    request_timeout_seconds: float = 20.0
    retry_delay_seconds: float = 0.35
    max_retries: int = 2


class ScanSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    global_settings: GlobalScanSettingsPayload = Field(default_factory=GlobalScanSettingsPayload, alias="global")
    sites: dict[str, SiteScanSettingsPayload] = Field(default_factory=dict)
    watchlist: "WatchlistSettingsPayload" = Field(default_factory=lambda: WatchlistSettingsPayload())


class WatchlistSiteSettingsPayload(BaseModel):
    enabled: bool = True
    interval_seconds: float = 8.0
    max_concurrency: int = 1
    request_timeout_seconds: float = 12.0
    jitter_seconds: float = 0.5


class WatchlistSettingsPayload(BaseModel):
    enabled: bool = True
    backoff_on_429: bool = True
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 300.0
    pause_site_on_error: bool = False
    sites: dict[str, WatchlistSiteSettingsPayload] = Field(default_factory=dict)


class WatchlistPatchRequest(BaseModel):
    title: str | None = None
    url: str | None = None
    image_url: str | None = None
    price_value: float | None = None
    current_inventory_status: str | None = None
    status_confidence_score: float | None = None
    pinned: bool | None = None
    enabled: bool | None = None
    orphaned: bool | None = None
    source: str | None = None
    last_error: str | None = None


class WatchlistManualRequest(BaseModel):
    site: str
    product_key: str | None = None
    article_number: str | None = None
    sku: str | None = None
    handle: str | None = None
    title: str = ""
    url: str
    pinned: bool = True


class WatchlistSyncRequest(BaseModel):
    site: str | None = None
    product_key: str | None = None
