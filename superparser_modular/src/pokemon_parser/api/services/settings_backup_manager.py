from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from pokemon_parser.api.services.filters_manager import FiltersManager
from pokemon_parser.api.services.shared import storage_context
from pokemon_parser.config import AppConfig
from pokemon_parser.filters.models import FilterRule
from pokemon_parser.models import WatchlistProduct
from pokemon_parser.parsers import SITE_LABELS
from pokemon_parser.storage.sqlite import SqliteStorage
from pokemon_parser.utils.time import utc_now_iso

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BACKUP_APP_NAME = "pokemon_parser"
SENSITIVE_BACKUP_KEYS = {
    "bot_token",
    "chat_id",
    "telegram_bot_token",
    "telegram_chat_id",
    "password",
    "proxy_password",
    "proxy_login",
    "login",
    "checkout_email",
    "checkout_first_name",
    "checkout_last_name",
    "checkout_street",
    "checkout_house_number",
    "checkout_zip_code",
    "checkout_city",
    "checkout_card_number",
    "checkout_card_expiry",
    "checkout_card_cvv",
    "checkout_card_name",
    "chrome_binary",
    "chrome_user_data_dir",
    "chrome_profile_dir",
    "selenium_test_url",
    "bol_buy_now_url",
}


@dataclass(frozen=True)
class SettingsBackupPaths:
    repo_root: Path
    app_root: Path


class SettingsBackupManager:
    def __init__(self, *, config_manager, paths: SettingsBackupPaths) -> None:
        self.config_manager = config_manager
        self.paths = paths
        self.filters_manager = FiltersManager(config_manager=config_manager)

    def build_download_filename(self) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        return f"pokemon_parser_settings_backup_{stamp}.json"

    def export_backup(self, *, include_watchlist_items: bool = True) -> dict[str, Any]:
        scan_payload = self.config_manager.get_scan_settings()
        cfg = self.config_manager.load_app_config()
        scan_settings = {
            "global": deepcopy(scan_payload.get("global", {})),
            "sites": deepcopy(scan_payload.get("sites", {})),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "exported_at": utc_now_iso(),
            "app": {
                "name": BACKUP_APP_NAME,
                "branch": self._git_branch(),
                "version": self._app_version(),
            },
            "scan_settings": scan_settings,
            "watchlist_settings": deepcopy(scan_payload.get("watchlist", {})),
            "channels": cfg.parser_enabled_map(),
            "filters": self._export_filters(),
            "watchlist_items": self._export_watchlist_items() if include_watchlist_items else [],
            "runtime_preferences": self._runtime_preferences(),
            "action_mode": self.config_manager.get_action_mode_settings(),
            "notification_preferences": self.config_manager.get_notifications_settings(),
            "worker_preferences": self._worker_preferences(),
            "network_preferences": self._network_preferences(),
        }

    def save_snapshot(self, *, prefix: str = "settings_backup_", include_watchlist_items: bool = True) -> dict[str, Any]:
        snapshot_dir = self.paths.app_root / "settings_snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        target = snapshot_dir / f"{prefix}{stamp}.json"
        payload = self.export_backup(include_watchlist_items=include_watchlist_items)
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {
            "ok": True,
            "path": str(target),
            "filename": target.name,
            "filters_count": len(payload.get("filters") or []),
            "watchlist_items_count": len(payload.get("watchlist_items") or []),
        }

    def preview_restore(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        backup = self._validate_backup(raw_payload)
        summary = self._build_restore_summary(backup)
        return {
            "ok": True,
            "valid": True,
            "schema_version": backup["schema_version"],
            "exported_at": backup.get("exported_at"),
            "app": backup.get("app") if isinstance(backup.get("app"), dict) else {},
            "backup_summary": {
                "filters_count": len(self._payload_list(backup, "filters")),
                "watchlist_items_count": len(self._payload_list(backup, "watchlist_items")),
            },
            **summary,
            "safety": self._safety_summary(),
        }

    def restore(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        backup = self._validate_backup(raw_payload)
        preview = self.preview_restore(backup)
        pre_restore_snapshot = self.save_snapshot(prefix="pre_restore_settings_backup_")

        applied: dict[str, Any] = {}
        if self._has_any(backup, "scan_settings", "watchlist_settings", "channels"):
            applied["scan_settings"] = self._restore_scan_settings(backup)
        if isinstance(backup.get("runtime_preferences"), dict):
            applied["runtime_preferences"] = self.config_manager.save_timer_settings(backup["runtime_preferences"])
        if isinstance(backup.get("action_mode"), dict):
            applied["action_mode"] = self.config_manager.save_action_mode_settings(backup["action_mode"].get("mode"))
        elif isinstance(backup.get("action_mode"), str):
            applied["action_mode"] = self.config_manager.save_action_mode_settings(backup["action_mode"])
        if isinstance(backup.get("notification_preferences"), dict):
            applied["notification_preferences"] = self.config_manager.save_notifications_settings(
                backup["notification_preferences"]
            )
        if isinstance(backup.get("worker_preferences"), dict):
            applied["worker_preferences"] = self.config_manager.save_worker_settings(backup["worker_preferences"])
        if isinstance(backup.get("network_preferences"), dict):
            applied["network_preferences"] = self._restore_network_preferences(backup["network_preferences"])

        filter_result = self._restore_filters(self._payload_list(backup, "filters"))
        watchlist_result = self._restore_watchlist_items(self._payload_list(backup, "watchlist_items"))
        applied["filters"] = filter_result
        applied["watchlist_items"] = watchlist_result

        result = {
            "ok": True,
            "message": "Settings restore applied.",
            "pre_restore_snapshot": pre_restore_snapshot,
            "preview": preview,
            "applied": applied,
        }
        self._log_restore_result(result)
        return result

    def _app_version(self) -> str:
        try:
            return metadata.version("pokemon-parser")
        except Exception:
            return "2.0.0"

    def _git_branch(self) -> str:
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=str(self.paths.repo_root),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return "unknown"
        branch = (result.stdout or "").strip()
        return branch or "unknown"

    def _runtime_preferences(self) -> dict[str, Any]:
        timer = self.config_manager.get_timer_settings()
        return {
            "enabled": bool(timer.get("enabled", False)),
            "interval": timer.get("interval", 15),
            "unit": timer.get("unit", "minutes"),
        }

    def _worker_preferences(self) -> dict[str, Any]:
        payload = dict(self.config_manager.get_worker_settings())
        payload.pop("worker_speed_profile_options", None)
        return payload

    def _network_preferences(self) -> dict[str, Any]:
        proxy = self.config_manager.get_proxy_settings()
        return {
            "enabled": bool(proxy.get("enabled", False)),
            "type": str(proxy.get("type") or "http"),
            "host": str(proxy.get("host") or ""),
            "port": int(proxy.get("port") or 0),
        }

    def _export_filters(self) -> list[dict[str, Any]]:
        return list(self.filters_manager.list_filters().get("items") or [])

    def _export_watchlist_items(self) -> list[dict[str, Any]]:
        cfg = self.config_manager.load_app_config()
        with storage_context(cfg) as storage:
            items = storage.list_watchlist(limit=2000)
        return [self._sanitize_watchlist_export_item(item) for item in items]

    @staticmethod
    def _sanitize_watchlist_export_item(item: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "site",
            "product_key",
            "article_number",
            "sku",
            "handle",
            "title",
            "url",
            "image_url",
            "price_value",
            "currency",
            "current_inventory_status",
            "status_confidence_score",
            "pinned",
            "enabled",
            "orphaned",
            "source",
        )
        return {key: deepcopy(item.get(key)) for key in keys if key in item}

    def _validate_backup(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw_payload, dict):
            raise ValueError("Settings backup must be a JSON object.")
        payload = raw_payload.get("backup") if isinstance(raw_payload.get("backup"), dict) else raw_payload
        if not isinstance(payload, dict):
            raise ValueError("Settings backup must be a JSON object.")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported settings backup schema_version={payload.get('schema_version')!r}.")
        self._reject_sensitive_values(payload)
        return payload

    def _reject_sensitive_values(self, payload: dict[str, Any]) -> None:
        # Backups produced by this service never include these keys. If a hand-edited
        # file contains them, restore ignores them, but preview reports the file as
        # still safe because private values are outside the accepted restore groups.
        for key in SENSITIVE_BACKUP_KEYS:
            if key in payload:
                logger.info("[settings-backup] ignoring private top-level key during restore: %s", key)

    @staticmethod
    def _has_any(payload: dict[str, Any], *keys: str) -> bool:
        return any(key in payload for key in keys)

    @staticmethod
    def _payload_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        value = payload.get(key)
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def _build_restore_summary(self, backup: dict[str, Any]) -> dict[str, Any]:
        current = self.export_backup(include_watchlist_items=True)
        groups = [
            self._group_summary("scan_settings", "Scan settings", backup, current),
            self._group_summary("watchlist_settings", "Watchlist settings", backup, current),
            self._group_summary("channels", "Parser/channel flags", backup, current),
            self._group_summary("runtime_preferences", "Runtime timer preferences", backup, current),
            self._group_summary("action_mode", "Action mode", backup, current),
            self._group_summary("notification_preferences", "Notification switches", backup, current),
            self._group_summary("worker_preferences", "Worker timing preferences", backup, current),
            self._group_summary("network_preferences", "Proxy preferences", backup, current),
            self._filter_preview_group(self._payload_list(backup, "filters")),
            self._watchlist_preview_group(self._payload_list(backup, "watchlist_items")),
        ]
        groups = [group for group in groups if group["present"]]
        return {
            "groups": groups,
            "groups_changed": [group["key"] for group in groups if group["will_change"]],
            "will_change": any(group["will_change"] for group in groups),
        }

    def _group_summary(
        self,
        key: str,
        label: str,
        backup: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        present = key in backup and isinstance(backup.get(key), (dict, list, str, int, float, bool, type(None)))
        will_change = False
        if present:
            will_change = self._normalized(backup.get(key)) != self._normalized(current.get(key))
        return {
            "key": key,
            "label": label,
            "present": present,
            "will_change": will_change,
            "summary": "Changes detected" if will_change else "Already matches current settings",
        }

    def _filter_preview_group(self, filters: list[dict[str, Any]]) -> dict[str, Any]:
        plan = self._plan_filter_upserts(filters)
        return {
            "key": "filters",
            "label": "Filters",
            "present": bool(filters),
            "will_change": bool(plan["created"] or plan["updated"]),
            "summary": f"{plan['created']} new, {plan['updated']} updated, {plan['unchanged']} unchanged",
            "counts": plan,
        }

    def _watchlist_preview_group(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        plan = self._plan_watchlist_upserts(items)
        return {
            "key": "watchlist_items",
            "label": "Watchlist entries",
            "present": bool(items),
            "will_change": bool(plan["created"] or plan["updated"]),
            "summary": f"{plan['created']} new, {plan['updated']} updated, {plan['unchanged']} unchanged",
            "counts": plan,
        }

    @staticmethod
    def _normalized(value: Any) -> Any:
        return json.loads(json.dumps(value, sort_keys=True, default=str))

    def _restore_scan_settings(self, backup: dict[str, Any]) -> dict[str, Any]:
        current = self.config_manager.get_scan_settings()
        payload = {
            "global": deepcopy(current.get("global", {})),
            "sites": deepcopy(current.get("sites", {})),
            "watchlist": deepcopy(current.get("watchlist", {})),
        }
        incoming_scan = backup.get("scan_settings") if isinstance(backup.get("scan_settings"), dict) else {}
        incoming_watchlist = (
            backup.get("watchlist_settings") if isinstance(backup.get("watchlist_settings"), dict) else {}
        )
        self._deep_update(payload, incoming_scan)
        if incoming_watchlist:
            self._deep_update(payload.setdefault("watchlist", {}), incoming_watchlist)
        channels = backup.get("channels") if isinstance(backup.get("channels"), dict) else {}
        for site, enabled in channels.items():
            site_key = str(site).strip().lower()
            if site_key in SITE_LABELS:
                payload.setdefault("sites", {}).setdefault(site_key, {})["enabled"] = bool(enabled)
        return self.config_manager.save_scan_settings(payload)

    def _restore_network_preferences(self, preferences: dict[str, Any]) -> dict[str, Any]:
        current = self.config_manager.get_proxy_settings()
        payload = {
            "enabled": bool(preferences.get("enabled", current.get("enabled", False))),
            "type": str(preferences.get("type", current.get("type", "http")) or "http"),
            "host": str(preferences.get("host", current.get("host", "")) or ""),
            "port": int(preferences.get("port", current.get("port", 0)) or 0),
            "login": str(current.get("login", "") or ""),
            "password": str(current.get("password", "") or ""),
        }
        return self.config_manager.save_proxy_settings(payload)

    @staticmethod
    def _deep_update(target: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                SettingsBackupManager._deep_update(target[key], value)
            else:
                target[key] = deepcopy(value)
        return target

    def _plan_filter_upserts(self, filters: list[dict[str, Any]], *, apply: bool = False) -> dict[str, int]:
        cfg = self.config_manager.load_app_config()
        created = updated = unchanged = skipped = 0
        with storage_context(cfg) as storage:
            existing = storage.list_filters_all()
            by_id = {rule.id: rule for rule in existing}
            by_name = {self._filter_name_key(rule.name): rule for rule in existing if rule.name}
            for item in filters:
                try:
                    desired = FiltersManager._normalize_filter_payload(item)
                except Exception:
                    skipped += 1
                    continue
                if not desired.name:
                    skipped += 1
                    continue

                target = by_id.get(desired.id) if desired.id else None
                target = target or by_name.get(self._filter_name_key(desired.name))
                if target is None:
                    created += 1
                    if apply:
                        created_rule = storage.create_filter(self._copy_filter(desired, filter_id=0))
                        by_id[created_rule.id] = created_rule
                        by_name[self._filter_name_key(created_rule.name)] = created_rule
                    continue

                desired_for_target = self._copy_filter(desired, filter_id=target.id)
                if self._filter_signature(target) == self._filter_signature(desired_for_target):
                    unchanged += 1
                    continue

                updated += 1
                if apply:
                    updated_rule = storage.update_filter(desired_for_target)
                    if updated_rule is not None:
                        by_id[updated_rule.id] = updated_rule
                        by_name[self._filter_name_key(updated_rule.name)] = updated_rule
        return {"created": created, "updated": updated, "unchanged": unchanged, "skipped": skipped}

    def _restore_filters(self, filters: list[dict[str, Any]]) -> dict[str, int]:
        return self._plan_filter_upserts(filters, apply=True)

    @staticmethod
    def _copy_filter(rule: FilterRule, *, filter_id: int) -> FilterRule:
        return FilterRule(
            id=filter_id,
            name=rule.name,
            sites=rule.sites,
            include_groups=rule.include_groups,
            exclude_words=rule.exclude_words,
            min_price=rule.min_price,
            max_price=rule.max_price,
            soft_price=rule.soft_price,
            enabled=rule.enabled,
        )

    @staticmethod
    def _filter_name_key(name: str) -> str:
        return str(name or "").strip().lower()

    @staticmethod
    def _filter_signature(rule: FilterRule) -> tuple[Any, ...]:
        return (
            rule.name,
            tuple(rule.sites),
            tuple(tuple(group) for group in rule.include_groups),
            tuple(rule.exclude_words),
            rule.min_price,
            rule.max_price,
            bool(rule.soft_price),
            bool(rule.enabled),
        )

    def _plan_watchlist_upserts(self, items: list[dict[str, Any]], *, apply: bool = False) -> dict[str, int]:
        cfg = self.config_manager.load_app_config()
        created = updated = unchanged = skipped = 0
        with storage_context(cfg) as storage:
            existing_items = storage.list_watchlist(limit=2000)
            by_key = {
                (str(item.get("site") or ""), str(item.get("product_key") or "")): item
                for item in existing_items
                if item.get("site") and item.get("product_key")
            }
            by_url = {
                (str(item.get("site") or ""), str(item.get("url") or "")): item
                for item in existing_items
                if item.get("site") and item.get("url")
            }
            for raw_item in items:
                item = self._normalize_watchlist_item(raw_item)
                if item is None:
                    skipped += 1
                    continue
                target = by_key.get((item["site"], item["product_key"])) or by_url.get((item["site"], item["url"]))
                if target is not None:
                    item["product_key"] = str(target.get("product_key") or item["product_key"])
                if target is None:
                    created += 1
                    if apply:
                        saved = self._upsert_watchlist_item(storage, item)
                        by_key[(saved["site"], saved["product_key"])] = saved
                        by_url[(saved["site"], saved["url"])] = saved
                    continue

                if self._watchlist_signature(target) == self._watchlist_signature(item):
                    unchanged += 1
                    continue

                updated += 1
                if apply:
                    saved = self._upsert_watchlist_item(storage, item)
                    by_key[(saved["site"], saved["product_key"])] = saved
                    by_url[(saved["site"], saved["url"])] = saved
        return {"created": created, "updated": updated, "unchanged": unchanged, "skipped": skipped}

    def _restore_watchlist_items(self, items: list[dict[str, Any]]) -> dict[str, int]:
        return self._plan_watchlist_upserts(items, apply=True)

    @staticmethod
    def _normalize_watchlist_item(raw_item: dict[str, Any]) -> dict[str, Any] | None:
        site = str(raw_item.get("site") or "").strip().lower()
        if site not in SITE_LABELS:
            return None
        url = str(raw_item.get("url") or "").strip()
        product_key = str(
            raw_item.get("product_key")
            or raw_item.get("sku")
            or raw_item.get("article_number")
            or raw_item.get("handle")
            or url
        ).strip()
        if not site or not product_key or not url:
            return None

        def _float_or_none(value: Any) -> float | None:
            if value in {None, ""}:
                return None
            try:
                return float(value)
            except Exception:
                return None

        return {
            "site": site,
            "product_key": product_key,
            "article_number": raw_item.get("article_number") or None,
            "sku": raw_item.get("sku") or None,
            "handle": raw_item.get("handle") or None,
            "title": str(raw_item.get("title") or product_key),
            "url": url,
            "image_url": raw_item.get("image_url") or None,
            "price_value": _float_or_none(raw_item.get("price_value")),
            "currency": str(raw_item.get("currency") or "EUR"),
            "current_inventory_status": str(raw_item.get("current_inventory_status") or "unknown"),
            "status_confidence_score": _float_or_none(raw_item.get("status_confidence_score")) or 0.0,
            "pinned": bool(raw_item.get("pinned", False)),
            "enabled": bool(raw_item.get("enabled", True)),
            "orphaned": bool(raw_item.get("orphaned", False)),
            "source": str(raw_item.get("source") or "imported"),
        }

    @staticmethod
    def _watchlist_signature(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            str(item.get("site") or ""),
            str(item.get("product_key") or ""),
            str(item.get("article_number") or ""),
            str(item.get("sku") or ""),
            str(item.get("handle") or ""),
            str(item.get("title") or ""),
            str(item.get("url") or ""),
            str(item.get("image_url") or ""),
            item.get("price_value"),
            str(item.get("currency") or "EUR"),
            bool(item.get("pinned", False)),
            bool(item.get("enabled", True)),
            bool(item.get("orphaned", False)),
            str(item.get("source") or ""),
        )

    def _upsert_watchlist_item(self, storage: SqliteStorage, item: dict[str, Any]) -> dict[str, Any]:
        product = WatchlistProduct(
            site=item["site"],
            product_key=item["product_key"],
            article_number=item.get("article_number"),
            sku=item.get("sku"),
            handle=item.get("handle"),
            title=item["title"],
            url=item["url"],
            image_url=item.get("image_url"),
            price_value=item.get("price_value"),
            currency=item.get("currency") or "EUR",
            current_inventory_status=item.get("current_inventory_status") or "unknown",
            status_confidence_score=float(item.get("status_confidence_score") or 0.0),
            pinned=bool(item.get("pinned", False)),
            enabled=bool(item.get("enabled", True)),
            orphaned=bool(item.get("orphaned", False)),
            source=item.get("source") or "imported",
        )
        saved = storage.upsert_watchlist_entry(product)
        return storage.update_watchlist_item(
            saved["id"],
            {
                "title": item["title"],
                "url": item["url"],
                "image_url": item.get("image_url"),
                "price_value": item.get("price_value"),
                "current_inventory_status": item.get("current_inventory_status") or "unknown",
                "status_confidence_score": float(item.get("status_confidence_score") or 0.0),
                "pinned": bool(item.get("pinned", False)),
                "enabled": bool(item.get("enabled", True)),
                "orphaned": bool(item.get("orphaned", False)),
                "source": item.get("source") or "imported",
            },
        ) or saved

    def _log_restore_result(self, result: dict[str, Any]) -> None:
        try:
            cfg: AppConfig = self.config_manager.load_app_config()
            with sqlite3.connect(str(cfg.resolved_db_path()), check_same_thread=False) as conn:
                storage = SqliteStorage(conn)
                storage.init_schema()
                conn.execute(
                    """
                    INSERT INTO runtime_logs (level, category, site, message, details_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "INFO",
                        "settings_backup",
                        None,
                        "settings_restore_applied",
                        json.dumps(
                            {
                                "pre_restore_snapshot": result.get("pre_restore_snapshot", {}).get("path"),
                                "groups_changed": result.get("preview", {}).get("groups_changed", []),
                                "applied": result.get("applied", {}),
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                        utc_now_iso(),
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("[settings-backup] failed to log restore result")

    @staticmethod
    def _safety_summary() -> dict[str, Any]:
        return {
            "private_data_included": False,
            "deletes_filters_or_watchlist": False,
            "creates_pre_restore_snapshot": True,
            "omitted": [
                "private account data",
                "payment and checkout details",
                "browser profile and session data",
                "Telegram bot token and chat id",
                "proxy login and password",
                "sensitive local machine paths",
            ],
        }
