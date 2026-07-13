from __future__ import annotations

from typing import Iterable

from pokemon_parser.filters.models import FilterRule
from pokemon_parser.models import ParsedItem
from pokemon_parser.utils.text import normalize_text


def _tokenize(text: str) -> set[str]:
    return set((text or "").split())


def match_precheck(item: ParsedItem, rule: FilterRule) -> bool:
    if not rule.enabled:
        return False
    if rule.sites and item.site not in rule.sites:
        return False

    words = _tokenize(item.title_norm)
    if rule.include_groups:
        group_hit = False
        for group in rule.include_groups:
            normalized = [normalize_text(word) for word in group if normalize_text(word)]
            if normalized and all(word in words for word in normalized):
                group_hit = True
                break
        if not group_hit:
            return False

    for word in rule.exclude_words:
        if normalize_text(word) in words:
            return False

    return True


def match(item: ParsedItem, rule: FilterRule) -> bool:
    if not match_precheck(item, rule):
        return False

    if item.price_value is None:
        return rule.soft_price

    if rule.min_price is not None and item.price_value < rule.min_price:
        return False
    if rule.max_price is not None and item.price_value > rule.max_price:
        return False
    return True


def explain(item: ParsedItem, rule: FilterRule) -> list[str]:
    reasons: list[str] = []
    words = _tokenize(item.title_norm)

    for group in rule.include_groups:
        normalized = [normalize_text(word) for word in group if normalize_text(word)]
        if normalized and all(word in words for word in normalized):
            reasons.append(f"keywords matched={normalized}")
            break

    if item.price_value is None:
        reasons.append("price missing -> soft price mode" if rule.soft_price else "price missing -> hard fail")
    else:
        if rule.min_price is not None:
            reasons.append(f"price >= min ({item.price_value} >= {rule.min_price}) = {item.price_value >= rule.min_price}")
        if rule.max_price is not None:
            reasons.append(f"price <= max ({item.price_value} <= {rule.max_price}) = {item.price_value <= rule.max_price}")

    return reasons
