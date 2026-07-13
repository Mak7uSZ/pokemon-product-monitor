from __future__ import annotations

import json
from pathlib import Path

from pokemon_parser.filters.models import FilterRule
from pokemon_parser.utils.text import clean_text, normalize_text


def _normalize_group(raw_group) -> tuple[str, ...]:
    return tuple(
        normalized
        for normalized in (normalize_text(value) for value in raw_group or [])
        if normalized
    )


def load_filters_from_json(path: Path) -> list[FilterRule]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("filters.json must contain a top-level list")

    filters: list[FilterRule] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue

        groups = [
            _normalize_group(group)
            for group in item.get("keyword_groups") or []
        ]
        filters.append(
            FilterRule(
                id=int(item.get("id", idx) or idx),
                name=clean_text(item.get("name")),
                sites=tuple(
                    str(site).strip().lower()
                    for site in item.get("sites") or []
                    if str(site).strip()
                ),
                include_groups=tuple(group for group in groups if group),
                exclude_words=tuple(
                    normalized
                    for normalized in (normalize_text(value) for value in item.get("exclude_words") or [])
                    if normalized
                ),
                min_price=item.get("min_price"),
                max_price=item.get("max_price"),
                soft_price=bool(item.get("soft_price", True)),
                enabled=bool(item.get("enabled", True)),
            )
        )

    return filters
