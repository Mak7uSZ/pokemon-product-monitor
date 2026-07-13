import sqlite3

from pokemon_parser.models import ActionTarget, AddToCartTarget
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.workers.pocketgames_worker import PocketGamesWorkerCase
from pokemon_parser.workers.purchase_safety import (
    BLOCKING_PURCHASE_STATUSES,
    PURCHASE_STATUS_CONFIRMED,
    PURCHASE_STATUS_FAILED,
    PURCHASE_STATUS_QUEUED,
    PURCHASE_STATUS_UNKNOWN_REVIEW,
    purchase_key_for_target,
)


def _target(handle: str = "pokemon-booster") -> ActionTarget:
    return ActionTarget(
        site="pocketgames",
        external_id=handle,
        title="Pokemon Booster",
        product_url=f"https://pocketgames.nl/products/{handle}",
        add_to_cart=AddToCartTarget(
            type="shopify_variant",
            product_id="123",
            variant_id=456,
            product_url=f"https://pocketgames.nl/products/{handle}",
        ),
    )


class _DummyDriver:
    def __init__(self, *, url: str, text: str, source: str | None = None):
        self.current_url = url
        self.title = ""
        self.page_source = source if source is not None else f"<html><body>{text}</body></html>"
        self._text = text

    def execute_script(self, script: str, *args):
        if "document.body" in script:
            return self._text
        return None

    def find_elements(self, by, selector):
        return []


def test_pocketgames_purchase_key_uses_stable_handle():
    target = _target("scarlet-violet-booster")

    assert purchase_key_for_target(target) == "pocketgames::scarlet-violet-booster"


def test_purchase_state_blocks_queued_confirmed_and_unknown_results():
    storage = SqliteStorage(sqlite3.connect(":memory:"))
    storage.init_schema()
    target = _target()
    purchase_key = purchase_key_for_target(target)

    reserved, _ = storage.reserve_purchase_state(
        site=target.site,
        purchase_key=purchase_key,
        external_id=target.external_id,
        title=target.title,
        product_url=target.product_url,
        status=PURCHASE_STATUS_QUEUED,
        blocking_statuses=BLOCKING_PURCHASE_STATUSES,
    )
    assert reserved is True

    duplicate_reserved, existing = storage.reserve_purchase_state(
        site=target.site,
        purchase_key=purchase_key,
        external_id=target.external_id,
        title=target.title,
        product_url=target.product_url,
        status=PURCHASE_STATUS_QUEUED,
        blocking_statuses=BLOCKING_PURCHASE_STATUSES,
    )
    assert duplicate_reserved is False
    assert existing is not None
    assert existing["status"] == PURCHASE_STATUS_QUEUED

    storage.update_purchase_state(
        site=target.site,
        purchase_key=purchase_key,
        status=PURCHASE_STATUS_CONFIRMED,
    )
    confirmed_reserved, _ = storage.reserve_purchase_state(
        site=target.site,
        purchase_key=purchase_key,
        external_id=target.external_id,
        title=target.title,
        product_url=target.product_url,
        status=PURCHASE_STATUS_QUEUED,
        blocking_statuses=BLOCKING_PURCHASE_STATUSES,
    )
    assert confirmed_reserved is False

    storage.update_purchase_state(
        site=target.site,
        purchase_key=purchase_key,
        status=PURCHASE_STATUS_UNKNOWN_REVIEW,
    )
    unknown_reserved, _ = storage.reserve_purchase_state(
        site=target.site,
        purchase_key=purchase_key,
        external_id=target.external_id,
        title=target.title,
        product_url=target.product_url,
        status=PURCHASE_STATUS_QUEUED,
        blocking_statuses=BLOCKING_PURCHASE_STATUSES,
    )
    assert unknown_reserved is False

    storage.update_purchase_state(
        site=target.site,
        purchase_key=purchase_key,
        status=PURCHASE_STATUS_FAILED,
    )
    retry_reserved, _ = storage.reserve_purchase_state(
        site=target.site,
        purchase_key=purchase_key,
        external_id=target.external_id,
        title=target.title,
        product_url=target.product_url,
        status=PURCHASE_STATUS_QUEUED,
        blocking_statuses=BLOCKING_PURCHASE_STATUSES,
    )
    assert retry_reserved is True


def test_pocketgames_purchase_detection_requires_confirmation_signal():
    driver = _DummyDriver(
        url="https://pocketgames.nl/checkouts/cn/thank-you",
        text="Bedankt voor je bestelling. Bestelnummer #1001",
    )

    status, signal = PocketGamesWorkerCase._detect_purchase_result(driver)

    assert status == PURCHASE_STATUS_CONFIRMED
    assert signal.startswith(("text:", "url:"))


def test_pocketgames_purchase_detection_does_not_confirm_order_summary():
    driver = _DummyDriver(
        url="https://pocketgames.nl/checkouts/cn/payment",
        text="Your order summary Payment Shipping",
    )

    status, signal = PocketGamesWorkerCase._detect_purchase_result(driver)

    assert status == PURCHASE_STATUS_UNKNOWN_REVIEW
    assert signal == "confirmation_not_detected"


def test_pocketgames_purchase_detection_classifies_payment_failure():
    driver = _DummyDriver(
        url="https://pocketgames.nl/checkouts/cn/payment",
        text="Betaling mislukt. Controleer je kaartgegevens.",
    )

    status, signal = PocketGamesWorkerCase._detect_purchase_result(driver)

    assert status == PURCHASE_STATUS_FAILED
    assert signal == "text:betaling mislukt"
