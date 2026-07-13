from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "release_guard.py"
SPEC = importlib.util.spec_from_file_location("release_guard", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
release_guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_guard
SPEC.loader.exec_module(release_guard)


def test_path_policy_allows_templates_and_rejects_runtime_state():
    assert release_guard.prohibited_path_reason("superparser_modular/.env.example") is None
    assert release_guard.prohibited_path_reason("superparser_modular/filters.example.json") is None
    assert release_guard.prohibited_path_reason("superparser_modular/.env") == "environment-file"
    assert release_guard.prohibited_path_reason("superparser_modular/private.db") == "prohibited-file-type"
    assert release_guard.prohibited_path_reason("superparser_modular/chrome-profile/Cookies") == "runtime-directory"
    assert release_guard.prohibited_path_reason("diagnostics/session.har") == "runtime-directory"


def test_content_policy_reports_categories_without_secret_values():
    token = "123456789:" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    findings = release_guard.scan_text("fixture.txt", f"TELEGRAM_BOT_TOKEN={token}\n")

    assert [finding.category for finding in findings] == ["telegram-token"]
    assert token not in findings[0].safe_message()


def test_content_policy_allows_reserved_example_identifiers():
    text = "contact=maintainer@example.invalid\nurl=https://service.example.test/hook\nhost=127.0.0.1\n"
    assert release_guard.scan_text("docs/example.txt", text) == []


def test_content_policy_rejects_escaped_local_workspace_paths():
    workspace_path = "F:" + "\\\\" + ("Docu" + "ments") + "\\\\private-project"
    findings = release_guard.scan_text("test.py", f'path = "{workspace_path}"')
    assert [finding.category for finding in findings] == ["absolute-workspace-path"]


def test_filesystem_scan_rejects_database_and_accepts_safe_source(tmp_path):
    (tmp_path / "safe.py").write_text("value = 'synthetic'\n", encoding="utf-8")
    (tmp_path / "private.db").write_bytes(b"not a real database")

    findings = release_guard.scan_paths(tmp_path, release_guard.filesystem_paths(tmp_path))

    assert [(finding.category, finding.path) for finding in findings] == [
        ("prohibited-file-type", "private.db")
    ]
