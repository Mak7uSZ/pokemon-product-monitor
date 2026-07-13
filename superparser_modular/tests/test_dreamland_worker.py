from pokemon_parser.models import ActionTarget, CheckoutTarget
from pokemon_parser.parsers.dreamland import DreamLandParser
from pokemon_parser.workers.dreamland_worker import DreamLandWorkerCase


class _DummyDriver:
    def __init__(self, current_url: str, page_text: str, visible_css: set[str] | None = None):
        self.current_url = current_url
        self._page_text = page_text
        self._visible_css = visible_css or set()

    def execute_script(self, script: str, *args):
        if "document.body && document.body.innerText" in script:
            return self._page_text
        raise RuntimeError("unsupported script")

    def find_elements(self, by, selector):
        if by == "css selector" and selector in self._visible_css:
            return [_DummyElement()]
        return []


class _DummyElement:
    def is_displayed(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    @property
    def rect(self):
        return {"width": 100, "height": 40}


def test_dreamland_build_target_uses_cart_url() -> None:
    parser = DreamLandParser()

    target = parser._build_target(
        external_id="02321530",
        title="Pokemon Test Product",
        product_url="https://www.dreamland.nl/producten/test/02321530",
        page=1,
        image_url=None,
        price_source="listing",
        availability_source="listing",
    )

    assert target.checkout is not None
    assert target.checkout.cart_url == "https://www.dreamland.nl/cart"


def test_dreamland_worker_rewrites_legacy_winkelmand_url() -> None:
    target = ActionTarget(
        site="dreamland",
        external_id="02321530",
        title="Pokemon Test Product",
        product_url="https://www.dreamland.nl/producten/test/02321530",
        checkout=CheckoutTarget(
            type="ui_flow",
            cart_url="https://www.dreamland.nl/winkelmand",
            checkout_url="https://www.dreamland.nl/checkout",
        ),
    )

    assert DreamLandWorkerCase._cart_url(target) == "https://www.dreamland.nl/cart"


def test_dreamland_worker_normalizes_card_expiry_formats() -> None:
    assert DreamLandWorkerCase._normalize_card_expiry("1225") == "12/25"
    assert DreamLandWorkerCase._normalize_card_expiry("12/25") == "12/25"
    assert DreamLandWorkerCase._normalize_card_expiry("12/2025") == "12/25"


def test_dreamland_worker_detects_login_page() -> None:
    driver = _DummyDriver(
        current_url="https://www.dreamland.nl/login",
        page_text="Al klant bij DreamLand? E-mail Wachtwoord Inloggen",
        visible_css={
            "button#submit, button[data-login-button]",
            'input[type="password"]',
        },
    )

    assert DreamLandWorkerCase._is_login_page(driver) is True
