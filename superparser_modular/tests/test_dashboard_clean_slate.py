from __future__ import annotations

import json
from pathlib import Path

from fastapi import Request

from pokemon_parser.api.app import safe_frontend_file
from pokemon_parser.api.services.dashboard_clean_slate import (
    CLEAR_SITE_DATA_VALUE,
    DASHBOARD_CLEAN_SLATE_HEADER,
    DASHBOARD_CLEAN_SLATE_REASON_HEADER,
    build_clean_slate_html_response,
    build_clean_slate_json_response,
    build_manual_clean_slate_json_response,
    dashboard_clean_slate_reason,
)


def _request(path: str, *, cookie: str = "", query_string: str = "") -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "headers": headers,
            "query_string": query_string.encode("utf-8"),
        }
    )


def test_dashboard_api_oversized_cookie_returns_clean_slate_response():
    request = _request("/api/runtime/status", cookie=f"dashboard={'x' * 6200}; theme=dark")
    reason = dashboard_clean_slate_reason(request)
    response = build_clean_slate_json_response(request, reason=reason)

    assert response.status_code == 431
    assert json.loads(response.body)["reason"] == "cookie_header_too_large"
    assert response.headers[DASHBOARD_CLEAN_SLATE_HEADER] == "true"
    assert response.headers[DASHBOARD_CLEAN_SLATE_REASON_HEADER] == "cookie_header_too_large"
    assert response.headers["Clear-Site-Data"] == CLEAR_SITE_DATA_VALUE
    assert "dashboard=" in response.headers.get("set-cookie", "")


def test_dashboard_clean_slate_is_api_targeted():
    request = _request("/asset", cookie=f"dashboard={'x' * 6200}")

    assert dashboard_clean_slate_reason(request) is None


def test_manual_clean_slate_endpoint_sets_clear_site_data_without_worker_side_effects():
    request = _request("/api/system/dashboard-clean-slate", cookie="dashboard=stale; theme=dark")
    response = build_manual_clean_slate_json_response(request, reason="manual")

    assert response.status_code == 200
    assert json.loads(response.body)["ok"] is True
    assert response.headers[DASHBOARD_CLEAN_SLATE_HEADER] == "true"
    assert response.headers["Clear-Site-Data"] == CLEAR_SITE_DATA_VALUE
    assert "dashboard=" in response.headers.get("set-cookie", "")


def test_clean_slate_restart_page_clears_browser_storage_and_redirects():
    request = _request("/clean-slate", cookie="dashboard=stale", query_string="next=/runtime")
    response = build_clean_slate_html_response(request, reason="dashboard_restart")

    assert response.status_code == 200
    assert response.headers[DASHBOARD_CLEAN_SLATE_HEADER] == "true"
    body = response.body.decode("utf-8")
    assert "window.localStorage.clear()" in body
    assert "window.sessionStorage.clear()" in body
    assert "document.cookie" in body
    assert 'var nextPath = "/runtime";' in body


def test_dashboard_request_helper_only_cleans_state_on_explicit_server_signal():
    app_source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")

    assert "AbortController" in app_source
    assert "DASHBOARD_REQUEST_TIMEOUT_MS" in app_source
    assert "runDashboardCleanSlate()" in app_source
    assert "window.localStorage.clear()" in app_source
    assert "window.sessionStorage.clear()" in app_source
    assert "X-Dashboard-Clean-Slate" in app_source
    assert "cleanSlateOnTimeoutOption ?? false" in app_source


def test_logs_summary_timeout_does_not_trigger_clean_slate():
    app_source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")

    assert 'request("/api/logs/summary", { timeoutMs: 3500, cleanSlateOnTimeout: false })' in app_source
    assert "getLogsSummarySafe" in app_source
    assert "fallbackLogsSummary" in app_source


def test_spa_file_resolution_rejects_plain_and_encoded_traversal(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("dashboard", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=not-for-the-dashboard", encoding="utf-8")

    assert safe_frontend_file(frontend, "index.html") == (frontend / "index.html").resolve()
    assert safe_frontend_file(frontend, "../.env") is None
    assert safe_frontend_file(frontend, "%2e%2e/.env") is None
    assert safe_frontend_file(frontend, "%252e%252e/.env") is None
    assert safe_frontend_file(frontend, "..\\.env") is None
