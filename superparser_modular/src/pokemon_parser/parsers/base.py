from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod

import aiohttp

from pokemon_parser.config import AppConfig
from pokemon_parser.models import ParsedItem


class BaseParser(ABC):
    site: str

    def request_timeout_seconds(self, cfg: AppConfig) -> float:
        return cfg.site_request_timeout_seconds(self.site)

    def max_retries(self, cfg: AppConfig) -> int:
        return cfg.max_retries()

    def max_pages(self, cfg: AppConfig) -> int | None:
        return cfg.site_max_pages(self.site)

    def page_delay_seconds(self, cfg: AppConfig) -> float:
        return cfg.site_page_delay_seconds(self.site)

    async def sleep_retry(self, cfg: AppConfig, attempt: int) -> None:
        base_delay = cfg.retry_delay_seconds()
        jitter = min(0.15, max(0.0, base_delay * 0.5))
        await asyncio.sleep(base_delay * (attempt + 1) + random.uniform(0.0, jitter))

    @abstractmethod
    async def fetch(self, session: aiohttp.ClientSession, cfg: AppConfig) -> list[ParsedItem]:
        raise NotImplementedError
