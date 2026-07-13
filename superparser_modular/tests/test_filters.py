from pokemon_parser.filters.engine import match
from pokemon_parser.filters.models import FilterRule
from pokemon_parser.models import ParsedItem
from pokemon_parser.utils.text import normalize_text


def test_filter_match():
    rule = FilterRule(
        id=1,
        name="pokemon booster",
        sites=("bol",),
        include_groups=(("pokemon", "booster"),),
        min_price=5.0,
        max_price=20.0,
        soft_price=True,
        enabled=True,
    )
    item = ParsedItem(
        site="bol",
        external_id="x",
        title="Pokemon Booster Bundle",
        title_norm="pokemon booster bundle",
        url="https://example.com",
        price_value=12.99,
        availability_text="Op voorraad",
        is_available=True,
        seller="bol",
    )
    assert match(item, rule) is True


def test_filter_soft_price():
    rule = FilterRule(
        id=1,
        name="pokemon",
        sites=("bol",),
        include_groups=(("pokemon",),),
        soft_price=True,
        enabled=True,
    )
    item = ParsedItem(
        site="bol",
        external_id="x",
        title="Pokemon Something",
        title_norm="pokemon something",
        url="https://example.com",
        price_value=None,
        availability_text=None,
        is_available=True,
        seller=None,
    )
    assert match(item, rule) is True


def test_filter_matches_accented_pokemon_title():
    rule = FilterRule(
        id=1,
        name="pokemon",
        sites=("dreamland",),
        include_groups=(("pokemon",),),
        enabled=True,
    )
    item = ParsedItem(
        site="dreamland",
        external_id="x",
        title="Pokémon Booster",
        title_norm=normalize_text("Pokémon Booster"),
        url="https://example.com",
        price_value=12.99,
        availability_text="Op voorraad",
        is_available=True,
        seller="dreamland",
    )

    assert match(item, rule) is True
