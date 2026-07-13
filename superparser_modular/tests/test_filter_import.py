import json
import sqlite3
from types import SimpleNamespace

from pokemon_parser.api.services.filters_manager import FiltersManager
from pokemon_parser.filters.legacy import load_filters_from_json
from pokemon_parser.storage.sqlite import SqliteStorage


def test_load_filters_from_json_preserves_sites_and_optional_fields(tmp_path):
    path = tmp_path / "filters.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "Dreamland Toploader filter",
                    "sites": ["dreamland"],
                    "keyword_groups": [["toploader", "transparant"]],
                    "exclude_words": ["damaged"],
                    "min_price": 8,
                    "max_price": 9,
                    "soft_price": False,
                    "enabled": True,
                }
            ]
        ),
        encoding="utf-8",
    )

    rules = load_filters_from_json(path)

    assert len(rules) == 1
    assert rules[0].sites == ("dreamland",)
    assert rules[0].exclude_words == ("damaged",)
    assert rules[0].soft_price is False


def test_import_filters_from_json_skips_duplicates(tmp_path):
    path = tmp_path / "filters.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "Pokemon Mega Evolution Base Booster Pack",
                    "sites": ["bol"],
                    "keyword_groups": [["pokemon", "mega", "evolution", "base", "booster", "pack"]],
                    "min_price": 5,
                    "max_price": 20,
                    "enabled": False,
                },
                {
                    "name": "Dreamland Toploader filter",
                    "sites": ["dreamland"],
                    "keyword_groups": [["toploader", "transparant"]],
                    "min_price": 8,
                    "max_price": 9,
                    "enabled": True,
                },
            ]
        ),
        encoding="utf-8",
    )

    storage = SqliteStorage(sqlite3.connect(":memory:"))
    storage.init_schema()
    manager = FiltersManager(config_manager=None)
    cfg = SimpleNamespace(filters_json_path=path)

    first = manager._import_legacy_filters(storage=storage, cfg=cfg)
    second = manager._import_legacy_filters(storage=storage, cfg=cfg)

    assert first["imported_count"] == 2
    assert first["total_count"] == 2
    assert second["imported_count"] == 0
    assert second["total_count"] == 2
