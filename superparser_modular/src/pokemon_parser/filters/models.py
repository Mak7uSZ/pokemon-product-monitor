from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FilterRule:
    id: int
    name: str
    sites: tuple[str, ...]
    include_groups: tuple[tuple[str, ...], ...]
    exclude_words: tuple[str, ...] = ()
    min_price: float | None = None
    max_price: float | None = None
    soft_price: bool = True
    enabled: bool = True
