from __future__ import annotations

from collections import OrderedDict

from pokemon_parser.config import AppConfig
from pokemon_parser.parsers.base import BaseParser
from pokemon_parser.parsers.bol import BolParser
from pokemon_parser.parsers.dreamland import DreamLandParser
from pokemon_parser.parsers.mediamarkt import MediaMarktParser
from pokemon_parser.parsers.pocketgames import PocketGamesParser

SITE_LABELS = {
    "mediamarkt": "MediaMarkt",
    "dreamland": "Dreamland",
    "bol": "Bol",
    "pocketgames": "PocketGames",
}


def build_parser_registry() -> "OrderedDict[str, BaseParser]":
    return OrderedDict(
        (
            ("mediamarkt", MediaMarktParser()),
            ("dreamland", DreamLandParser()),
            ("bol", BolParser()),
            ("pocketgames", PocketGamesParser()),
        )
    )


def build_enabled_parser_registry(cfg: AppConfig) -> "OrderedDict[str, BaseParser]":
    registry = build_parser_registry()
    return OrderedDict(
        (site, parser)
        for site, parser in registry.items()
        if cfg.is_parser_enabled(site)
    )
