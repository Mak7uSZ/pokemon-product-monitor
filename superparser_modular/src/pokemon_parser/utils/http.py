from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiohttp


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    timeout_seconds: float = 10.0
    retry_base_delay: float = 1.0


class HttpClient:
    def __init__(self, session: aiohttp.ClientSession, policy: RetryPolicy | None = None):
        self.session = session
        self.policy = policy or RetryPolicy()

    async def get_text(self, url: str, **kwargs) -> str:
        timeout = kwargs.pop("timeout", aiohttp.ClientTimeout(total=self.policy.timeout_seconds))
        last_error: Exception | None = None

        for attempt in range(1, self.policy.attempts + 1):
            try:
                async with self.session.get(url, timeout=timeout, **kwargs) as response:
                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After")
                        wait_seconds = float(retry_after) if retry_after and retry_after.isdigit() else self.policy.retry_base_delay * attempt
                        await asyncio.sleep(wait_seconds)
                        continue

                    response.raise_for_status()
                    return await response.text()
            except Exception as exc:
                last_error = exc
                if attempt == self.policy.attempts:
                    raise
                await asyncio.sleep(self.policy.retry_base_delay * attempt)

        if last_error:
            raise last_error
        raise RuntimeError("unreachable")
