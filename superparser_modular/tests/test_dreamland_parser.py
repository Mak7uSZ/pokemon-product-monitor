import asyncio
from types import SimpleNamespace

from bs4 import BeautifulSoup

from pokemon_parser.dreamland_availability import detect_dreamland_availability
from pokemon_parser.engine.pipeline import Pipeline
from pokemon_parser.models import ParsedItem
from pokemon_parser.parsers.dreamland import DreamLandParser


def test_dreamland_listing_title_prefers_data_events_name_over_generic_anchor() -> None:
    parser = DreamLandParser()
    html = """
    <article class="product-card" data-events='[{"event":"addToCart","data":{"name":"Toploader Kaarthoezen Transparant en Super Dik 25 per verpakking (63 x 88 mm)"}}]'>
        <a href="/producten/toploader-kaarthoezen-transparant-en-super-dik-25-per-verpakking-63-x-88-mm/02321530">
            Filter producten
        </a>
    </article>
    """
    soup = BeautifulSoup(html, "html.parser")
    card = soup.select_one("article")
    anchor = soup.select_one("a")

    assert card is not None
    assert anchor is not None

    title = parser._extract_listing_title_from_card(
        card,
        anchor,
        "https://www.dreamland.nl/producten/toploader-kaarthoezen-transparant-en-super-dik-25-per-verpakking-63-x-88-mm/02321530",
    )

    assert title == "Toploader Kaarthoezen Transparant en Super Dik 25 per verpakking (63 x 88 mm)"


def test_dreamland_listing_title_falls_back_to_slug_when_anchor_is_generic() -> None:
    parser = DreamLandParser()
    html = """
    <article class="product-card">
        <a href="/producten/pokemon-scarlet-violet-3-5-mini-tin/01751000">
            Alleen online
        </a>
    </article>
    """
    soup = BeautifulSoup(html, "html.parser")
    card = soup.select_one("article")
    anchor = soup.select_one("a")

    assert card is not None
    assert anchor is not None

    title = parser._extract_listing_title_from_card(
        card,
        anchor,
        "https://www.dreamland.nl/producten/pokemon-scarlet-violet-3-5-mini-tin/01751000",
    )

    assert title == "pokemon scarlet violet 3 5 mini tin"


def test_dreamland_pdp_out_of_stock_text_is_not_purchasable() -> None:
    parser = DreamLandParser()
    soup = BeautifulSoup(
        """
        <html>
          <body>
            <h1>Pokémon ME 2.5 Ascended Heroes Bundle 6 Boosters ENG</h1>
            <span>€35,99</span>
            <span>ALLEEN ONLINE</span>
            <p>Online tijdelijk uitverkocht</p>
            <p>Weet als eerste als dit product op voorraad is.</p>
            <button>Hou me op de hoogte</button>
            <button>Toevoegen aan verlanglijstje</button>
          </body>
        </html>
        """,
        "html.parser",
    )

    availability = parser._parse_pdp_availability(soup)

    assert availability.purchasable is False
    assert availability.status == "unavailable"
    assert "online tijdelijk uitverkocht" in availability.reason


def test_dreamland_pdp_hard_negative_beats_listing_available_hint() -> None:
    class _Parser(DreamLandParser):
        async def fetch_product(self, session, cfg, product_url):
            return """
            <html>
              <head><link rel="canonical" href="https://www.dreamland.nl/producten/pokemon-test/02371610"></head>
              <body>
                <h1>Pokémon ME 2.5 Ascended Heroes Bundle 6 Boosters ENG</h1>
                <span>€35,99</span>
                <p>Online tijdelijk uitverkocht</p>
                <button>Hou me op de hoogte</button>
              </body>
            </html>
            """

    card = {
        "page": 1,
        "listing_title": "Pokémon ME 2.5 Ascended Heroes Bundle 6 Boosters ENG",
        "product_url": "https://www.dreamland.nl/producten/pokemon-test/02371610",
        "listing_price_value": 35.99,
        "image_url": None,
        "listing_is_available_hint": True,
        "listing_availability_hint_text": "huidige_levertijd",
    }

    item = asyncio.run(_Parser()._enrich_one(object(), asyncio.Semaphore(1), card, SimpleNamespace()))

    assert item is not None
    assert item.is_available is False
    assert item.target is None
    assert item.availability_text.startswith("text:online tijdelijk uitverkocht")
    assert item.extra["purchasable"] is False


def test_dreamland_wishlist_does_not_block_real_buy_cta() -> None:
    availability = detect_dreamland_availability(
        text="Toevoegen aan verlanglijstje Levering aan huis Huidige levertijd 1 tot 2 werkdagen",
        cta_texts=("Toevoegen aan verlanglijstje", "Levering aan huis"),
    )

    assert availability.purchasable is True
    assert any(signal == "cta:levering aan huis" for signal in availability.positive_signals)


def test_dreamland_price_and_online_only_do_not_imply_purchasable() -> None:
    availability = detect_dreamland_availability(
        text="Pokémon ME 2.5 Ascended Heroes Bundle 6 Boosters ENG €35,99 ALLEEN ONLINE",
    )

    assert availability.purchasable is False
    assert availability.status == "unknown"


def test_action_gate_blocks_dreamland_unavailable_filter_match() -> None:
    item = ParsedItem(
        site="dreamland",
        external_id="02371610",
        title="Pokémon ME 2.5 Ascended Heroes Bundle 6 Boosters ENG",
        title_norm="pokemon me 2 5 ascended heroes bundle 6 boosters eng",
        url="https://www.dreamland.nl/producten/pokemon-test/02371610",
        price_value=35.99,
        availability_text="text:online tijdelijk uitverkocht",
        is_available=False,
        seller="dreamland",
        extra={
            "purchasable": False,
            "availability_status": "unavailable",
            "availability_reason": "text:online tijdelijk uitverkocht",
            "negative_signals": ["text:online tijdelijk uitverkocht"],
        },
        target=None,
    )

    blocked = Pipeline._action_block_reason(item)

    assert blocked is not None
    reason, details = blocked
    assert reason == "dreamland_unavailable_signal"
    assert details["purchasable"] is False
