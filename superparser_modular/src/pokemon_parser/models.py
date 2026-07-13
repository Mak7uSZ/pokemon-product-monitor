from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

SiteName = Literal["bol", "pocketgames", "mediamarkt", "dreamland"]


@dataclass(frozen=True)
class AddToCartTarget:
    type: Literal["direct_url", "shopify_variant", "ui_button"]
    quantity: int = 1

    add_to_cart_url: Optional[str] = None
    product_id: Optional[int | str] = None
    offer_uid: Optional[str] = None
    variant_id: Optional[int] = None
    cart_add_url: Optional[str] = None
    cart_url: Optional[str] = None
    product_url: Optional[str] = None

    section_id: Optional[str] = None
    sections_url: Optional[str] = None

    pdp_button_selector: Optional[str] = None


@dataclass(frozen=True)
class CheckoutTarget:
    type: Literal["url", "shopify_cart", "ui_flow"]
    checkout_url: Optional[str] = None
    cart_url: Optional[str] = None


@dataclass(frozen=True)
class ActionTarget:
    site: SiteName
    external_id: str
    title: str
    product_url: str
    add_to_cart: Optional[AddToCartTarget] = None
    checkout: Optional[CheckoutTarget] = None
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedItem:
    site: SiteName
    external_id: str
    title: str
    title_norm: str
    url: str
    price_value: Optional[float]
    availability_text: Optional[str]
    is_available: bool
    seller: Optional[str]
    extra: Mapping[str, Any] = field(default_factory=dict)
    target: Optional[ActionTarget] = None


@dataclass(frozen=True)
class Event:
    site: SiteName
    external_id: str
    event_type: Literal[
        "new_item",
        "price_changed",
        "availability_changed",
        "restock",
        "seller_changed",
        "returned_to_listing",
        "disappeared",
    ]
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    matched_filter_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SeleniumJob:
    site: SiteName
    case: Literal["add_to_cart", "checkout", "add_to_cart_and_checkout"]
    target: ActionTarget
    action_id: str = ""
    created_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchlistProduct:
    site: SiteName
    product_key: str
    title: str
    url: str
    article_number: Optional[str] = None
    sku: Optional[str] = None
    handle: Optional[str] = None
    image_url: Optional[str] = None
    price_value: Optional[float] = None
    currency: str = "EUR"
    current_inventory_status: str = "unknown"
    status_confidence_score: float = 0.0
    matched_filter_ids: tuple[int, ...] = ()
    matched_filter_names: tuple[str, ...] = ()
    pinned: bool = False
    enabled: bool = True
    orphaned: bool = False
    source: str = "auto_filter_match"
    last_seen_at: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_available_at: Optional[str] = None
    last_status_change_at: Optional[str] = None
    last_error: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchlistCheckResult:
    site: SiteName
    product_key: str
    title: str
    url: str
    current_inventory_status: str
    status_confidence_score: float
    is_available: bool = False
    availability_text: Optional[str] = None
    source_endpoint: str = ""
    http_status: Optional[int] = None
    duration_seconds: float = 0.0
    error: Optional[str] = None
    item: Optional[ParsedItem] = None
    action_target: Optional[ActionTarget] = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class SeleniumState:
    started: bool = False
    ready: bool = False
    busy: bool = False
    lifecycle_state: str = "stopped"

    last_error: str = ""
    last_diagnostic_snapshot: str = ""
    last_job: str = ""
    last_result: str = ""
    last_duration_seconds: float = 0.0
    last_start_at: str | None = None
    last_stop_at: str | None = None
    browser_started_at: str | None = None
    last_driver_create_at: str | None = None
    last_driver_quit_at: str | None = None
    driver_session_id: str = ""
    chromedriver_pid: int | None = None
    chrome_pid: int | None = None
    tracked_chrome_pids: list[int] = field(default_factory=list)
    orphan_app_chrome_pids: list[int] = field(default_factory=list)
    selenium_window_count: int | None = None
    selenium_top_level_window_ids: list[int] = field(default_factory=list)
    selenium_top_level_window_count: int | None = None
    selenium_top_level_window_id_by_handle: dict[str, int] = field(default_factory=dict)
    window_handles_count: int = 0
    window_handles_current_urls: dict[str, str] = field(default_factory=dict)
    last_window_snapshot_at: str | None = None

    jobs_completed: int = 0
    jobs_failed: int = 0
    jobs_timed_out: int = 0
    driver_rebuilds: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    prewarmed: bool = False
    last_prewarm_skip_reason: str = ""
    last_prewarm_error: str = ""
    warm_tabs_enabled: bool = False
    warm_tabs_count: int = 0
    warm_tabs_max: int = 0
    warm_tab_urls: list[str] = field(default_factory=list)
    active_action: str = ""
    active_worker_action: str = ""
    warm_refresh_running: bool = False
    warm_refresh_paused_reason: str = ""
    challenge_detected_count: int = 0
    challenge_sources: dict[str, Any] = field(default_factory=dict)
    challenge_manual_action_required: bool = False
    last_action_latency_seconds: float = 0.0
    last_button_search_latency_seconds: float = 0.0
    duplicate_start_ignored_count: int = 0
    duplicate_start_guard_count: int = 0
