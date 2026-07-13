from __future__ import annotations

import logging
from typing import Any

from pokemon_parser.api.services.shared import storage_context
from pokemon_parser.config import parse_bool
from pokemon_parser.filters.legacy import load_filters_from_json
from pokemon_parser.filters.models import FilterRule
from pokemon_parser.utils.text import normalize_text

logger = logging.getLogger(__name__)


class FiltersManager:
    def __init__(self, *, config_manager):
        self.config_manager = config_manager

    @staticmethod
    def _normalize_keywords(value: Any) -> tuple[tuple[str, ...], ...]:
        groups: list[tuple[str, ...]] = []
        for raw_group in value or []:
            words = [normalize_text(word) for word in raw_group if normalize_text(word)]
            if words:
                groups.append(tuple(words))
        return tuple(groups)

    @staticmethod
    def _normalize_filter_payload(payload: dict[str, Any], *, filter_id: int = 0) -> FilterRule:
        return FilterRule(
            id=int(payload.get("id", filter_id) or filter_id or 0),
            name=str(payload.get("name", "") or "").strip(),
            sites=tuple(str(site).strip().lower() for site in payload.get("sites", []) if str(site).strip()),
            include_groups=FiltersManager._normalize_keywords(payload.get("keyword_groups")),
            exclude_words=tuple(
                normalize_text(word)
                for word in payload.get("exclude_words", [])
                if normalize_text(word)
            ),
            min_price=payload.get("min_price"),
            max_price=payload.get("max_price"),
            soft_price=parse_bool(str(payload.get("soft_price", True)), True),
            enabled=parse_bool(str(payload.get("enabled", True)), True),
        )

    @staticmethod
    def _serialize_filter(rule: FilterRule) -> dict[str, Any]:
        return {
            "id": rule.id,
            "name": rule.name,
            "sites": list(rule.sites),
            "keyword_groups": [list(group) for group in rule.include_groups],
            "exclude_words": list(rule.exclude_words),
            "min_price": rule.min_price,
            "max_price": rule.max_price,
            "soft_price": rule.soft_price,
            "enabled": rule.enabled,
        }

    @staticmethod
    def _rule_signature(rule: FilterRule) -> tuple[Any, ...]:
        return (
            rule.name.strip().lower(),
            tuple(sorted(site.strip().lower() for site in rule.sites)),
            tuple(tuple(word.strip().lower() for word in group) for group in rule.include_groups),
            tuple(sorted(word.strip().lower() for word in rule.exclude_words)),
            rule.min_price,
            rule.max_price,
            bool(rule.soft_price),
        )

    def _legacy_json_metadata(self, cfg) -> dict[str, Any]:
        path = cfg.filters_json_path
        metadata = {
            "exists": path.exists(),
            "path": path.name,
            "count": 0,
            "error": "",
        }
        if not metadata["exists"]:
            return metadata

        try:
            metadata["count"] = len(load_filters_from_json(path))
        except Exception:
            logger.warning("Unable to read legacy filters metadata", exc_info=True)
            metadata["error"] = "Legacy filters.json could not be read."
        return metadata

    def _import_legacy_filters(self, *, storage, cfg) -> dict[str, Any]:
        metadata = self._legacy_json_metadata(cfg)
        if not metadata["exists"]:
            return {
                "ok": False,
                "message": "Legacy filters.json was not found.",
                "imported_count": 0,
                "skipped_count": 0,
                "total_count": 0,
                "legacy_json": metadata,
            }
        if metadata["error"]:
            return {
                "ok": False,
                "message": metadata["error"],
                "imported_count": 0,
                "skipped_count": 0,
                "total_count": 0,
                "legacy_json": metadata,
            }

        legacy_rules = load_filters_from_json(cfg.filters_json_path)
        existing_rules = storage.list_filters_all()
        existing_signatures = {self._rule_signature(rule) for rule in existing_rules}
        seen_legacy_signatures: set[tuple[Any, ...]] = set()

        imported_count = 0
        skipped_count = 0
        for rule in legacy_rules:
            signature = self._rule_signature(rule)
            if signature in seen_legacy_signatures or signature in existing_signatures:
                skipped_count += 1
                continue

            seen_legacy_signatures.add(signature)
            storage.create_filter(
                FilterRule(
                    id=0,
                    name=rule.name,
                    sites=rule.sites,
                    include_groups=rule.include_groups,
                    exclude_words=rule.exclude_words,
                    min_price=rule.min_price,
                    max_price=rule.max_price,
                    soft_price=rule.soft_price,
                    enabled=rule.enabled,
                )
            )
            existing_signatures.add(signature)
            imported_count += 1

        total_count = len(storage.list_filters_all())
        message = (
            f"Imported {imported_count} legacy filter(s) from filters.json."
            if imported_count
            else "SQLite filters are already in sync with filters.json."
        )
        return {
            "ok": True,
            "message": message,
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "total_count": total_count,
            "legacy_json": metadata,
        }

    def list_filters(self) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        auto_import = {
            "attempted": False,
            "imported_count": 0,
            "skipped_count": 0,
            "message": "",
        }
        with storage_context(cfg) as storage:
            rules = storage.list_filters_all()
            if not rules and cfg.filters_json_path.exists():
                result = self._import_legacy_filters(storage=storage, cfg=cfg)
                auto_import = {
                    "attempted": True,
                    "imported_count": result.get("imported_count", 0),
                    "skipped_count": result.get("skipped_count", 0),
                    "message": result.get("message", ""),
                }
                rules = storage.list_filters_all()

        return {
            "items": [self._serialize_filter(rule) for rule in rules],
            "source": "sqlite",
            "legacy_json": self._legacy_json_metadata(cfg),
            "auto_import": auto_import,
        }

    def get_filter(self, filter_id: int) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        with storage_context(cfg) as storage:
            rule = storage.get_filter(filter_id)
        if rule is None:
            raise KeyError(f"filter {filter_id} not found")
        return {"item": self._serialize_filter(rule)}

    def create_filter(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        with storage_context(cfg) as storage:
            rule = storage.create_filter(self._normalize_filter_payload(payload))
        return {"item": self._serialize_filter(rule)}

    def update_filter(self, filter_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        with storage_context(cfg) as storage:
            rule = storage.update_filter(self._normalize_filter_payload(payload, filter_id=filter_id))
        if rule is None:
            raise KeyError(f"filter {filter_id} not found")
        return {"item": self._serialize_filter(rule)}

    def delete_filter(self, filter_id: int) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        with storage_context(cfg) as storage:
            deleted = storage.delete_filter(filter_id)
        return {"ok": deleted}

    def toggle_filter(self, filter_id: int) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        with storage_context(cfg) as storage:
            rule = storage.toggle_filter(filter_id)
        if rule is None:
            raise KeyError(f"filter {filter_id} not found")
        return {"item": self._serialize_filter(rule)}

    def import_filters_from_json(self) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        with storage_context(cfg) as storage:
            result = self._import_legacy_filters(storage=storage, cfg=cfg)
        result["source"] = "sqlite"
        return result
