from __future__ import annotations

from typing import Any

from pokemon_parser.api.services.shared import storage_context
from pokemon_parser.parsers import SITE_LABELS


class ParserSettingsManager:
    def __init__(self, *, config_manager, runtime_manager):
        self.config_manager = config_manager
        self.runtime_manager = runtime_manager

    def list_parsers(self) -> dict[str, Any]:
        cfg = self.config_manager.load_app_config()
        overview = self.runtime_manager.build_overview()
        site_states = overview.get("site_states", {})

        with storage_context(cfg) as storage:
            stats = storage.site_product_stats()

        items: list[dict[str, Any]] = []
        for site, label in SITE_LABELS.items():
            runtime_item = dict(site_states.get(site, {}))
            stat_item = stats.get(
                site,
                {
                    "product_count": 0,
                    "active_product_count": 0,
                    "in_stock_count": 0,
                    "out_of_stock_count": 0,
                    "event_count": 0,
                    "last_seen": None,
                },
            )
            items.append(
                {
                    "site": site,
                    "label": label,
                    "enabled": bool(cfg.is_parser_enabled(site)),
                    "status": runtime_item.get("status", "idle"),
                    "active": bool(runtime_item.get("active", False)),
                    "message": runtime_item.get("message", ""),
                    "last_run_at": runtime_item.get("last_run_at"),
                    "last_success_at": runtime_item.get("last_success_at"),
                    "last_error_at": runtime_item.get("last_error_at"),
                    "last_error": runtime_item.get("last_error", ""),
                    "last_items_found": runtime_item.get("last_items_found", 0),
                    "last_events_found": runtime_item.get("last_events_found", 0),
                    "next_in_seconds": runtime_item.get("next_in_seconds", 0.0),
                    "cooldown_in_seconds": runtime_item.get("cooldown_in_seconds", 0.0),
                    "runs_started": runtime_item.get("runs_started", 0),
                    "runs_completed": runtime_item.get("runs_completed", 0),
                    "successes": runtime_item.get("successes", 0),
                    "failures": runtime_item.get("failures", 0),
                    "skips": runtime_item.get("skips", 0),
                    "graphql_circuit_open": runtime_item.get("graphql_circuit_open", False),
                    "graphql_backoff_until": runtime_item.get("graphql_backoff_until"),
                    "discovery_routing_mode": runtime_item.get("discovery_routing_mode", "normal"),
                    "product_count": stat_item.get("product_count", 0),
                    "active_product_count": stat_item.get("active_product_count", 0),
                    "in_stock_count": stat_item.get("in_stock_count", 0),
                    "out_of_stock_count": stat_item.get("out_of_stock_count", 0),
                    "event_count": stat_item.get("event_count", 0),
                    "last_seen": stat_item.get("last_seen"),
                }
            )

        return {"items": items}

    def toggle_parser(self, site: str) -> dict[str, Any]:
        self.config_manager.toggle_parser(site)
        restarted = self.runtime_manager.restart_if_running(reason=f"parser toggle updated ({site})")
        listing = self.list_parsers()
        item = next((parser_item for parser_item in listing["items"] if parser_item["site"] == site), None)
        return {
            "item": item,
            "runtime_restarted": restarted,
        }
