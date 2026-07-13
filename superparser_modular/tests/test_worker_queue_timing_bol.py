import asyncio
from types import SimpleNamespace

from pokemon_parser.models import ActionTarget, AddToCartTarget, ParsedItem
from pokemon_parser.parsers.bol import BolParser
from pokemon_parser.workers.queue import detect_queue_page
from pokemon_parser.workers.timing import build_worker_timing


class _DummyDriver:
    current_url = "https://example.com/queue/waiting-room"
    title = "Please wait"
    page_source = "<html><body>You are in line. Even geduld.</body></html>"

    def execute_script(self, script: str, *args):
        if "document.body" in script:
            return "You are in line. Even geduld."
        return None

    def find_elements(self, by, selector):
        return []


def test_shared_queue_detection_uses_url_and_text_signals():
    state = detect_queue_page(_DummyDriver(), "dreamland")

    assert state.in_queue is True
    assert any(signal.startswith("url:") for signal in state.signals)
    assert any(signal.startswith("text:") for signal in state.signals)


def test_worker_timing_fast_profile_reduces_click_pause():
    cfg = SimpleNamespace(
        worker_speed_profile="fast",
        worker_click_pause_seconds=0.2,
        worker_after_navigation_wait_seconds=0.5,
        worker_after_add_to_cart_wait_seconds=0.6,
        worker_after_checkout_click_wait_seconds=0.6,
        worker_wait_timeout_seconds=20.0,
        worker_poll_seconds=0.2,
        worker_retry_pause_seconds=0.45,
    )

    timing = build_worker_timing(cfg)

    assert timing.click_pause_seconds < 0.2
    assert timing.poll_seconds < 0.2


def test_bol_enrich_accepts_cfg_and_builds_add_to_cart_url():
    offer_uid = "123e4567-e89b-12d3-a456-426614174000"

    class _Parser(BolParser):
        async def fetch_product(self, session, cfg, product_url):
            return f'{{"offerUid":"{offer_uid}"}}'

    item = ParsedItem(
        site="bol",
        external_id="bol_product",
        title="Pokemon Test",
        title_norm="pokemon test",
        url="https://www.bol.com/nl/nl/p/test/1234567890/",
        price_value=10.0,
        availability_text="possible_available",
        is_available=True,
        seller="bol",
        target=ActionTarget(
            site="bol",
            external_id="bol_product",
            title="Pokemon Test",
            product_url="https://www.bol.com/nl/nl/p/test/1234567890/",
            add_to_cart=AddToCartTarget(
                type="direct_url",
                product_id="1234567890",
                product_url="https://www.bol.com/nl/nl/p/test/1234567890/",
            ),
        ),
    )

    enriched = asyncio.run(_Parser().enrich(object(), item, SimpleNamespace()))

    assert enriched.target is not None
    assert enriched.target.add_to_cart is not None
    assert offer_uid in (enriched.target.add_to_cart.add_to_cart_url or "")
