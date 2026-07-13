from __future__ import annotations

import logging

import aiohttp
import requests

from pokemon_parser.utils.proxy import build_requests_proxy_map
from pokemon_parser.utils.logging_setup import redact_secrets

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def is_enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def build_payload(self, text: str) -> dict[str, object]:
        return {
            "chat_id": self.chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }

    def build_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    async def send(self, session: aiohttp.ClientSession, text: str, *, metadata: dict | None = None) -> None:
        log_metadata = redact_secrets(metadata or {})
        logger.info(
            "[telegram] send attempt enabled=%s metadata=%s",
            self.is_enabled(),
            log_metadata,
        )
        if not self.is_enabled():
            logger.info("[telegram] send skipped enabled=false metadata=%s", log_metadata)
            return

        try:
            async with session.post(self.build_url(), json=self.build_payload(text), timeout=10) as response:
                response.raise_for_status()
                body = await response.text()
                logger.info(
                    "[telegram] send success status=%s metadata=%s response_length=%s",
                    response.status,
                    log_metadata,
                    len(body),
                )
        except Exception:
            logger.exception("[telegram] send failed metadata=%s", log_metadata)
            raise

    def send_sync(
        self,
        text: str,
        *,
        proxy_cfg: object | None = None,
        timeout: float = 10.0,
        metadata: dict | None = None,
    ) -> None:
        log_metadata = redact_secrets(metadata or {})
        logger.info(
            "[telegram] send_sync attempt enabled=%s metadata=%s",
            self.is_enabled(),
            log_metadata,
        )
        if not self.is_enabled():
            logger.info("[telegram] send_sync skipped enabled=false metadata=%s", log_metadata)
            return

        proxies = build_requests_proxy_map(proxy_cfg) if proxy_cfg is not None else None
        try:
            response = requests.post(
                self.build_url(),
                json=self.build_payload(text),
                timeout=timeout,
                proxies=proxies or None,
            )
            response.raise_for_status()
            logger.info(
                "[telegram] send_sync success status=%s metadata=%s response_length=%s",
                response.status_code,
                log_metadata,
                len(response.text or ""),
            )
        except Exception:
            logger.exception("[telegram] send_sync failed metadata=%s", log_metadata)
            raise
