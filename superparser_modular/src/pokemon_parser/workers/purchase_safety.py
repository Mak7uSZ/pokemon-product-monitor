from __future__ import annotations

import re

from pokemon_parser.models import ActionTarget
from pokemon_parser.utils.text import normalize_text

PURCHASE_STATUS_NOT_STARTED = "not_started"
PURCHASE_STATUS_QUEUED = "queued"
PURCHASE_STATUS_RUNNING = "running"
PURCHASE_STATUS_ADDED_TO_CART = "added_to_cart"
PURCHASE_STATUS_CHECKOUT_STARTED = "checkout_started"
PURCHASE_STATUS_PAYMENT_SUBMITTED = "payment_submitted"
PURCHASE_STATUS_CONFIRMED = "purchase_confirmed"
PURCHASE_STATUS_ALREADY_PURCHASED = "already_purchased"
PURCHASE_STATUS_DUPLICATE_SKIPPED = "duplicate_skipped"
PURCHASE_STATUS_FAILED = "failed"
PURCHASE_STATUS_UNKNOWN_REVIEW = "unknown_needs_review"

BLOCKING_PURCHASE_STATUSES = {
    PURCHASE_STATUS_QUEUED,
    PURCHASE_STATUS_RUNNING,
    PURCHASE_STATUS_PAYMENT_SUBMITTED,
    PURCHASE_STATUS_CONFIRMED,
    PURCHASE_STATUS_UNKNOWN_REVIEW,
}


def _safe_key_part(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"[^a-z0-9._:/-]+", "_", text)
    return text.strip("_")[:240]


def purchase_key_for_target(target: ActionTarget) -> str:
    identity = target.external_id

    if not identity and target.add_to_cart is not None:
        identity = target.add_to_cart.product_id or target.add_to_cart.variant_id

    if not identity:
        identity = target.product_url

    if not identity:
        identity = normalize_text(target.title)

    safe_identity = _safe_key_part(identity)
    return f"{target.site}::{safe_identity or 'unknown'}"


def duplicate_skip_status(existing_status: str | None) -> str:
    if existing_status == PURCHASE_STATUS_CONFIRMED:
        return PURCHASE_STATUS_ALREADY_PURCHASED
    if existing_status == PURCHASE_STATUS_UNKNOWN_REVIEW:
        return PURCHASE_STATUS_UNKNOWN_REVIEW
    return PURCHASE_STATUS_DUPLICATE_SKIPPED
