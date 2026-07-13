from __future__ import annotations

import json
import logging
from http.cookies import SimpleCookie
from typing import Iterable
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

logger = logging.getLogger(__name__)

DASHBOARD_CLEAN_SLATE_HEADER = "X-Dashboard-Clean-Slate"
DASHBOARD_CLEAN_SLATE_REASON_HEADER = "X-Dashboard-Clean-Slate-Reason"
DASHBOARD_COOKIE_BYTES_HEADER = "X-Dashboard-Cookie-Bytes"
DASHBOARD_HEADER_BYTES_HEADER = "X-Dashboard-Header-Bytes"
DASHBOARD_COOKIE_SAFE_BYTES = 6_144
DASHBOARD_HEADER_SAFE_BYTES = 14_336
CLEAR_SITE_DATA_VALUE = '"cache", "cookies", "storage"'


def _safe_int_text(value: int) -> str:
    return str(max(0, int(value or 0)))


def request_header_bytes(headers: Iterable[tuple[bytes, bytes]]) -> int:
    total = 0
    for name, value in headers:
        total += len(name or b"") + len(value or b"") + 4
    return total


def cookie_header_text(request: Request) -> str:
    return request.headers.get("cookie", "") or ""


def cookie_header_bytes(request: Request) -> int:
    return len(cookie_header_text(request).encode("utf-8", errors="ignore"))


def dashboard_clean_slate_reason(request: Request) -> str | None:
    if not request.url.path.startswith("/api/"):
        return None

    cookie_bytes = cookie_header_bytes(request)
    if cookie_bytes > DASHBOARD_COOKIE_SAFE_BYTES:
        return "cookie_header_too_large"

    header_bytes = request_header_bytes(request.scope.get("headers") or [])
    if header_bytes > DASHBOARD_HEADER_SAFE_BYTES:
        return "request_headers_too_large"

    return None


def _dashboard_cookie_names(cookie_header: str) -> list[str]:
    names: list[str] = []
    try:
        parsed = SimpleCookie()
        parsed.load(cookie_header)
        names.extend(str(name) for name in parsed.keys())
    except Exception:
        for part in cookie_header.split(";"):
            name = part.split("=", 1)[0].strip()
            if name:
                names.append(name)
    return sorted({name for name in names if name})


def apply_clean_slate_headers(
    response: Response,
    *,
    reason: str,
    cookie_header: str = "",
    cookie_bytes: int = 0,
    header_bytes: int = 0,
) -> Response:
    response.headers["Clear-Site-Data"] = CLEAR_SITE_DATA_VALUE
    response.headers[DASHBOARD_CLEAN_SLATE_HEADER] = "true"
    response.headers[DASHBOARD_CLEAN_SLATE_REASON_HEADER] = reason
    response.headers[DASHBOARD_COOKIE_BYTES_HEADER] = _safe_int_text(cookie_bytes)
    response.headers[DASHBOARD_HEADER_BYTES_HEADER] = _safe_int_text(header_bytes)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Access-Control-Expose-Headers"] = ", ".join(
        [
            DASHBOARD_CLEAN_SLATE_HEADER,
            DASHBOARD_CLEAN_SLATE_REASON_HEADER,
            DASHBOARD_COOKIE_BYTES_HEADER,
            DASHBOARD_HEADER_BYTES_HEADER,
        ]
    )

    for name in _dashboard_cookie_names(cookie_header):
        response.delete_cookie(name, path="/")
    return response


def build_clean_slate_json_response(
    request: Request,
    *,
    reason: str,
    status_code: int = 431,
) -> JSONResponse:
    cookie_header = cookie_header_text(request)
    cookie_bytes = len(cookie_header.encode("utf-8", errors="ignore"))
    header_bytes = request_header_bytes(request.scope.get("headers") or [])
    logger.warning(
        "dashboard_clean_slate_required reason=%s path=%s cookie_bytes=%s header_bytes=%s",
        reason,
        request.url.path,
        cookie_bytes,
        header_bytes,
    )
    response = JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "clean_slate_required": True,
            "reason": reason,
            "cookie_header_bytes": cookie_bytes,
            "request_header_bytes": header_bytes,
            "message": "Dashboard browser state was cleared; retry the request.",
        },
    )
    return apply_clean_slate_headers(
        response,
        reason=reason,
        cookie_header=cookie_header,
        cookie_bytes=cookie_bytes,
        header_bytes=header_bytes,
    )


def build_manual_clean_slate_json_response(request: Request, *, reason: str = "manual") -> JSONResponse:
    response = JSONResponse(
        content={
            "ok": True,
            "clean_slate_required": False,
            "reason": reason,
            "message": "Dashboard cookies and browser storage were asked to clear for this origin.",
        },
    )
    cookie_header = cookie_header_text(request)
    return apply_clean_slate_headers(
        response,
        reason=reason,
        cookie_header=cookie_header,
        cookie_bytes=len(cookie_header.encode("utf-8", errors="ignore")),
        header_bytes=request_header_bytes(request.scope.get("headers") or []),
    )


def build_clean_slate_html_response(request: Request, *, reason: str = "dashboard_restart") -> HTMLResponse:
    requested_next = request.query_params.get("next") or "/"
    next_path = requested_next if requested_next.startswith("/") and not requested_next.startswith("//") else "/"
    next_json = json.dumps(next_path)
    encoded_reason = quote(reason)
    body = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta http-equiv="cache-control" content="no-store" />
    <title>Dashboard Clean Slate</title>
  </head>
  <body>
    <script>
      (function () {{
        try {{ window.localStorage.clear(); }} catch (error) {{}}
        try {{ window.sessionStorage.clear(); }} catch (error) {{}}
        try {{
          document.cookie.split(";").forEach(function (cookie) {{
            var name = cookie.split("=")[0].trim();
            if (name) {{
              document.cookie = name + "=; Max-Age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Lax";
            }}
          }});
        }} catch (error) {{}}
        var nextPath = {next_json};
        window.location.replace(nextPath + (nextPath.indexOf("?") === -1 ? "?" : "&") + "cleanSlate={encoded_reason}");
      }})();
    </script>
  </body>
</html>"""
    response = HTMLResponse(content=body)
    cookie_header = cookie_header_text(request)
    return apply_clean_slate_headers(
        response,
        reason=reason,
        cookie_header=cookie_header,
        cookie_bytes=len(cookie_header.encode("utf-8", errors="ignore")),
        header_bytes=request_header_bytes(request.scope.get("headers") or []),
    )
