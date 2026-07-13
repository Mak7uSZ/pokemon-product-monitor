from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from pokemon_parser.config import AppConfig
from pokemon_parser.models import SeleniumJob
from pokemon_parser.notifications.telegram import TelegramNotifier
from pokemon_parser.storage.sqlite import SqliteStorage

logger = logging.getLogger(__name__)

TRACE_LEVELS = {
    "minimal": 0,
    "normal": 1,
    "verbose": 2,
}


class WorkerTraceLogger:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        job: SeleniumJob,
        storage: SqliteStorage | None = None,
        notifier: TelegramNotifier | None = None,
        action_id: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.job = job
        self.storage = storage
        self.notifier = notifier or TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
        self.action_id = action_id or f"{job.site}-{uuid.uuid4().hex[:8]}"
        self.started_at = time.monotonic()
        self.step_count = 0
        self.last_step = ""
        self.trace_level = TRACE_LEVELS.get(cfg.worker_telegram_trace_level, TRACE_LEVELS["normal"])
        self.result_status: str | None = None

    @property
    def site(self) -> str:
        return self.job.site

    def _duration(self) -> float:
        return round(time.monotonic() - self.started_at, 1)

    def _level_allowed(self, level: str) -> bool:
        return self.trace_level >= TRACE_LEVELS.get(level, TRACE_LEVELS["normal"])

    def _safe_url(self, metadata: dict[str, Any] | None = None) -> str:
        if metadata and metadata.get("url"):
            return str(metadata["url"])
        return self.job.target.product_url or ""

    def _metadata(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = {
            "action_id": self.action_id,
            "case": self.job.case,
            "external_id": self.job.target.external_id,
            "title": self.job.target.title,
        }
        if metadata:
            merged.update(metadata)
        return merged

    def _log(self, level: str, message: str, metadata: dict[str, Any] | None = None) -> None:
        details = self._metadata(metadata)
        log_method = getattr(logger, level.lower(), logger.info)
        log_method("[worker_trace][%s] action_id=%s %s", self.site, self.action_id, message)

        if self.storage is None:
            return
        try:
            self.storage.insert_runtime_log(
                level=level,
                category="worker_trace",
                site=self.site,
                message=message,
                details=details,
            )
        except Exception:
            logger.exception("[worker_trace][%s] failed to persist trace log", self.site)

    def _telegram_enabled(self) -> bool:
        return (
            self.cfg.enable_notifications
            and self.cfg.worker_telegram_trace_enabled
            and self.notifier.is_enabled()
        )

    def _send_telegram(self, text: str) -> None:
        if not self._telegram_enabled():
            return
        try:
            self.notifier.send_sync(text, proxy_cfg=self.cfg, timeout=8.0)
        except Exception as exc:
            logger.warning(
                "[worker_trace][%s] telegram send failed action_id=%s error=%s",
                self.site,
                self.action_id,
                exc,
            )
            if self.storage is not None:
                try:
                    self.storage.insert_runtime_log(
                        level="WARNING",
                        category="worker_trace",
                        site=self.site,
                        message=f"telegram trace send failed error={exc}",
                        details={"action_id": self.action_id},
                    )
                except Exception:
                    logger.exception("[worker_trace][%s] failed to persist telegram failure", self.site)

    def _format_block(self, title: str, metadata: dict[str, Any] | None = None) -> str:
        data = self._metadata(metadata)
        lines = [
            title,
            f"Site: {self.site.title()}",
            f"Product: {self.job.target.title}",
            f"URL: {self._safe_url(data)}",
            f"Mode: selenium",
            f"Action ID: {self.action_id}",
        ]

        if data.get("phase"):
            lines.append(f"Phase: {data['phase']}")
        if data.get("reason"):
            lines.append(f"Reason: {data['reason']}")
        if data.get("result"):
            lines.append(f"Result: {data['result']}")
        if data.get("duration_seconds") is not None:
            lines.append(f"Duration: {data['duration_seconds']}s")

        return "\n".join(lines)

    def start(self, metadata: dict[str, Any] | None = None) -> None:
        self._log("INFO", "Worker started", metadata)
        self._send_telegram(self._format_block("🤖 Worker started", metadata))

    def step(self, message: str, metadata: dict[str, Any] | None = None, *, level: str = "normal") -> None:
        if not self._level_allowed(level):
            return
        self.step_count += 1
        self.last_step = message
        self._log("INFO", message, metadata)
        data = self._metadata(metadata)
        lines = [
            f"➡️ {self.site.title()} step {self.step_count}",
            message,
            f"Action ID: {self.action_id}",
        ]
        if data.get("phase"):
            lines.append(f"Phase: {data['phase']}")
        if data.get("url"):
            lines.append(f"URL: {data['url']}")
        self._send_telegram("\n".join(lines))

    def success(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        data = self._metadata(metadata)
        data.setdefault("duration_seconds", self._duration())
        self._log("INFO", message, data)
        self._send_telegram(self._format_block("✅ Worker success", {**data, "result": message}))

    def set_result(self, status: str, metadata: dict[str, Any] | None = None) -> None:
        self.result_status = status
        self._log("INFO", f"Worker result state={status}", metadata)

    def warning(
        self,
        message: str,
        metadata: dict[str, Any] | None = None,
        *,
        level: str = "normal",
    ) -> None:
        self._log("WARNING", message, metadata)
        if not self._level_allowed(level):
            return
        data = self._metadata(metadata)
        lines = [
            "⏳ Worker warning" if "queue" in message.lower() else "⚠️ Worker warning",
            f"Site: {self.site.title()}",
            f"Step: {self.last_step or message}",
            f"Message: {message}",
            f"Action ID: {self.action_id}",
        ]
        if data.get("phase"):
            lines.append(f"Phase: {data['phase']}")
        if data.get("url"):
            lines.append(f"URL: {data['url']}")
        if data.get("waiting_up_to_seconds") is not None:
            lines.append(f"Waiting up to: {data['waiting_up_to_seconds']}s")
        self._send_telegram("\n".join(lines))

    def error(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        data = self._metadata(metadata)
        data.setdefault("duration_seconds", self._duration())
        data.setdefault("step", self.last_step)
        self._log("ERROR", message, data)
        lines = [
            "❌ Worker failed",
            f"Site: {self.site.title()}",
            f"Step: {data.get('step') or self.last_step or '-'}",
            f"Reason: {data.get('reason') or message}",
            f"URL: {self._safe_url(data)}",
            f"Duration: {data['duration_seconds']}s",
            f"Action ID: {self.action_id}",
        ]
        self._send_telegram("\n".join(lines))

    def finish(self, result: str, metadata: dict[str, Any] | None = None) -> None:
        data = self._metadata(metadata)
        data.setdefault("duration_seconds", self._duration())
        data["result"] = result
        self._log("INFO", f"Worker action finished result={result}", data)
        if self._level_allowed("minimal"):
            self._send_telegram(
                self._format_block(
                    f"Worker action finished ({result})",
                    {"result": result, "duration_seconds": data["duration_seconds"]},
                )
            )

    def action_log_details(self, extra: dict[str, Any] | None = None) -> str:
        details = self._metadata(extra)
        details["duration_seconds"] = self._duration()
        return json.dumps(details, ensure_ascii=False)
