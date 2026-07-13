from __future__ import annotations

import contextvars
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger("pokemon_parser.http")
_CURRENT_SCAN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("pokemon_parser_scan_id", default="")
_CURRENT_SCAN_SITE: contextvars.ContextVar[str] = contextvars.ContextVar("pokemon_parser_scan_site", default="")


@dataclass(frozen=True)
class ProxySettings:
    enabled: bool
    proxy_type: str
    host: str
    port: int
    login: str = ""
    password: str = ""

    @property
    def url(self) -> str | None:
        if not self.enabled or not self.host or int(self.port or 0) <= 0:
            return None
        scheme = self.proxy_type if self.proxy_type in {"http", "https", "socks5"} else "http"
        return f"{scheme}://{self.host}:{int(self.port)}"


def proxy_settings_from_config(cfg: Any) -> ProxySettings:
    return ProxySettings(
        enabled=bool(getattr(cfg, "proxy_enabled", False)),
        proxy_type=str(getattr(cfg, "proxy_type", "http") or "http").lower(),
        host=str(getattr(cfg, "proxy_host", "") or "").strip(),
        port=int(getattr(cfg, "proxy_port", 0) or 0),
        login=str(getattr(cfg, "proxy_login", "") or "").strip(),
        password=str(getattr(cfg, "proxy_password", "") or "").strip(),
    )


def build_aiohttp_proxy_kwargs(cfg: Any) -> dict[str, Any]:
    settings = proxy_settings_from_config(cfg)
    if not settings.url:
        return {}

    kwargs: dict[str, Any] = {"proxy": settings.url}
    if settings.login and settings.password:
        kwargs["proxy_auth"] = aiohttp.BasicAuth(settings.login, settings.password)
    return kwargs


def build_requests_proxy_map(cfg: Any) -> dict[str, str]:
    settings = proxy_settings_from_config(cfg)
    if not settings.url:
        return {}
    return {
        "http": settings.url,
        "https": settings.url,
    }


def set_http_diagnostic_context(*, scan_id: str, site: str) -> tuple[contextvars.Token, contextvars.Token]:
    return (
        _CURRENT_SCAN_ID.set(scan_id),
        _CURRENT_SCAN_SITE.set(site),
    )


def reset_http_diagnostic_context(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    scan_token, site_token = tokens
    _CURRENT_SCAN_ID.reset(scan_token)
    _CURRENT_SCAN_SITE.reset(site_token)


class _LoggedResponse:
    def __init__(self, response: aiohttp.ClientResponse, *, method: str, url: str, started_at: float):
        self._response = response
        self._method = method
        self._url = url
        self._started_at = started_at
        self._logged_body = False

    def _log_body(self, *, body_length: int | None, body_type: str) -> None:
        if self._logged_body:
            return
        self._logged_body = True
        logger.info(
            "[http] scan_id=%s site=%s method=%s url=%s status=%s response_length=%s body_type=%s duration=%.3fs",
            _CURRENT_SCAN_ID.get(),
            _CURRENT_SCAN_SITE.get(),
            self._method,
            self._url,
            getattr(self._response, "status", None),
            body_length,
            body_type,
            time.monotonic() - self._started_at,
        )

    async def text(self, *args: Any, **kwargs: Any) -> str:
        body = await self._response.text(*args, **kwargs)
        cached = getattr(self._response, "_body", None)
        body_length = len(cached) if isinstance(cached, bytes) else len(body.encode("utf-8", errors="replace"))
        self._log_body(body_length=body_length, body_type="text")
        return body

    async def read(self, *args: Any, **kwargs: Any) -> bytes:
        body = await self._response.read(*args, **kwargs)
        self._log_body(body_length=len(body), body_type="bytes")
        return body

    async def json(self, *args: Any, **kwargs: Any) -> Any:
        payload = await self._response.json(*args, **kwargs)
        cached = getattr(self._response, "_body", None)
        if isinstance(cached, bytes):
            body_length = len(cached)
        else:
            try:
                body_length = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except Exception:
                body_length = None
        self._log_body(body_length=body_length, body_type="json")
        return payload

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


class _LoggedRequestContext:
    def __init__(self, inner: Any, *, method: str, url: str):
        self._inner = inner
        self._method = method.upper()
        self._url = url
        self._started_at = time.monotonic()

    async def __aenter__(self) -> _LoggedResponse:
        logger.info(
            "[http] scan_id=%s site=%s method=%s url=%s request_start",
            _CURRENT_SCAN_ID.get(),
            _CURRENT_SCAN_SITE.get(),
            self._method,
            self._url,
        )
        try:
            response = await self._inner.__aenter__()
        except Exception:
            logger.exception(
                "[http] scan_id=%s site=%s method=%s url=%s request_open_failed duration=%.3fs",
                _CURRENT_SCAN_ID.get(),
                _CURRENT_SCAN_SITE.get(),
                self._method,
                self._url,
                time.monotonic() - self._started_at,
            )
            raise
        logger.info(
            "[http] scan_id=%s site=%s method=%s url=%s status=%s content_length_header=%s headers_received duration=%.3fs",
            _CURRENT_SCAN_ID.get(),
            _CURRENT_SCAN_SITE.get(),
            self._method,
            self._url,
            response.status,
            response.headers.get("Content-Length"),
            time.monotonic() - self._started_at,
        )
        return _LoggedResponse(response, method=self._method, url=self._url, started_at=self._started_at)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if exc is not None:
            logger.exception(
                "[http] scan_id=%s site=%s method=%s url=%s request_failed duration=%.3fs",
                _CURRENT_SCAN_ID.get(),
                _CURRENT_SCAN_SITE.get(),
                self._method,
                self._url,
                time.monotonic() - self._started_at,
            )
        return await self._inner.__aexit__(exc_type, exc, tb)

    def __await__(self):
        return self._inner.__await__()


class ProxyAwareSession:
    def __init__(self, session: aiohttp.ClientSession, cfg: Any):
        self._session = session
        self._proxy_kwargs = build_aiohttp_proxy_kwargs(cfg)

    def _merge_proxy_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if not self._proxy_kwargs:
            return kwargs

        merged = dict(kwargs)
        if "proxy" not in merged:
            merged["proxy"] = self._proxy_kwargs["proxy"]
        if "proxy_auth" not in merged and "proxy_auth" in self._proxy_kwargs:
            merged["proxy_auth"] = self._proxy_kwargs["proxy_auth"]
        return merged

    def request(self, method: str, url: str, *args: Any, **kwargs: Any):
        return _LoggedRequestContext(
            self._session.request(method, url, *args, **self._merge_proxy_kwargs(kwargs)),
            method=method,
            url=url,
        )

    def get(self, url: str, *args: Any, **kwargs: Any):
        return _LoggedRequestContext(
            self._session.get(url, *args, **self._merge_proxy_kwargs(kwargs)),
            method="GET",
            url=url,
        )

    def post(self, url: str, *args: Any, **kwargs: Any):
        return _LoggedRequestContext(
            self._session.post(url, *args, **self._merge_proxy_kwargs(kwargs)),
            method="POST",
            url=url,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)
