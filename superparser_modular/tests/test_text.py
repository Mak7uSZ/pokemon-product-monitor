from pokemon_parser.utils.text import normalize_text, parse_price_eur


def test_parse_price_eur():
    assert parse_price_eur("12,99") == 12.99
    assert parse_price_eur("12.99") == 12.99
    assert parse_price_eur("1.299,00") == 1299.0
    assert parse_price_eur("12,-") == 12.0
    assert parse_price_eur("") is None


def test_normalize_text():
    assert normalize_text("Pokémon Mega Evolution!! Booster Pack") == "pokemon mega evolution booster pack"
    assert normalize_text("Pokémon pokemon pokémon") == "pokemon pokemon pokemon"
