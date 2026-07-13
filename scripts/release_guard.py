#!/usr/bin/env python3
"""Fail closed when public-release source contains prohibited runtime data.

This is a deliberately small, dependency-free complement to Gitleaks. It does
not replace a full-history secret scanner. Findings contain only a category,
path, and line number; matched values are never printed.
"""

from __future__ import annotations

import argparse
import ipaddress
import math
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


MAX_TEXT_BYTES = 5 * 1024 * 1024

SAFE_ENV_NAMES = {".env.example", ".env.sample", ".env.template"}
SAFE_FILTER_NAMES = {"filters.example.json", "filters.sample.json"}

PROHIBITED_DIRECTORY_NAMES = {
    ".vite",
    "audit_compare",
    "backups",
    "browser-profile",
    "browser_profile",
    "chrome-profile",
    "chrome_profile",
    "debug_artifacts",
    "debug_logs",
    "diagnostics",
    "log_extracts",
    "node_modules",
    "settings_snapshots",
}

PROHIBITED_BROWSER_BASENAMES = {
    "cookies",
    "cookies-journal",
    "history",
    "local state",
    "login data",
    "login data-journal",
    "preferences",
    "secure preferences",
    "web data",
    "web data-journal",
}

PROHIBITED_SUFFIXES = {
    ".7z",
    ".bak",
    ".backup",
    ".core",
    ".db",
    ".dmp",
    ".gz",
    ".har",
    ".key",
    ".kdbx",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".rar",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".zip",
}

TEXT_SUFFIXES = {
    "",
    ".bat",
    ".css",
    ".csv",
    ".env",
    ".html",
    ".in",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".lock",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vbs",
    ".xml",
    ".yaml",
    ".yml",
}

HIGH_CONFIDENCE_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("telegram-token", re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("webhook", re.compile(r"https://(?:hooks\.slack\.com/services|discord(?:app)?\.com/api/webhooks)/\S+", re.I)),
    ("authorization-header", re.compile(r"\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/-]{16,}", re.I)),
)

SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|auth(?:orization)?|bot[_-]?token|cookie|password|passwd|"
    r"private[_-]?key|proxy[_-]?password|secret|session[_-]?id|token|webhook)\b"
    r"\s*[=:]\s*([\"'])([^\r\n\"']{20,256})\1"
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.I)
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
WINDOWS_USER_PATH_RE = re.compile(r"\b[A-Z]:\\Users\\([^\\\s]+)", re.I)
WINDOWS_WORKSPACE_PATH_RE = re.compile(
    r"\b[A-Z]:\\(?:Desktop|Documents|Downloads|Projects|Repos|Repositories|Source)\\",
    re.I,
)
UNIX_USER_PATH_RE = re.compile(r"(?<![\w/])/(?:home|Users)/([^/\s]+)")
CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

PLACEHOLDER_MARKERS = {
    "changeme",
    "dummy",
    "example",
    "fixture",
    "placeholder",
    "redacted",
    "replace",
    "sample",
    "synthetic",
    "test",
}


@dataclass(frozen=True, order=True)
class Finding:
    category: str
    path: str
    line: int = 0

    def safe_message(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.category}: {location}"


def normalized_path(value: str | Path) -> str:
    normalized = str(value).replace("\\", "/")
    return normalized[2:] if normalized.startswith("./") else normalized


def prohibited_path_reason(relative_path: str) -> str | None:
    normalized = normalized_path(relative_path)
    path = PurePosixPath(normalized)
    lowered_parts = tuple(part.lower() for part in path.parts)
    name = path.name.lower()

    if name.startswith(".env") and name not in SAFE_ENV_NAMES:
        return "environment-file"
    if name in {"filters.json", "filters copy.json", "scan_settings.json", "config.json"}:
        return "runtime-configuration"
    if any(part in PROHIBITED_DIRECTORY_NAMES for part in lowered_parts[:-1]):
        return "runtime-directory"
    if name in PROHIBITED_BROWSER_BASENAMES:
        return "browser-session-file"
    if name.endswith(("-wal", "-shm", "-journal", ".startup-migration.lock")):
        return "database-runtime-file"
    if path.suffix.lower() in PROHIBITED_SUFFIXES:
        return "prohibited-file-type"
    if name.startswith(("errors_tail", "mediamarkt_", "scan_decision_")) and path.suffix.lower() == ".txt":
        return "private-diagnostic"
    return None


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        not value
        or value.startswith(("${", "{{", "<"))
        or any(marker in lowered for marker in PLACEHOLDER_MARKERS)
    )


def luhn_valid(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def scan_text(relative_path: str, text: str) -> list[Finding]:
    findings: set[Finding] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        normalized_line = line.replace("\\\\", "\\")
        for category, pattern in HIGH_CONFIDENCE_PATTERNS:
            if pattern.search(line):
                findings.add(Finding(category, relative_path, line_number))

        for match in SENSITIVE_ASSIGNMENT_RE.finditer(line):
            candidate = match.group(2)
            if not looks_like_placeholder(candidate) and shannon_entropy(candidate) >= 4.1:
                findings.add(Finding("high-entropy-secret-assignment", relative_path, line_number))

        for match in EMAIL_RE.finditer(line):
            domain = match.group(1).lower()
            if not domain.endswith(("example.com", "example.org", "example.net", ".example", ".invalid", ".test")):
                findings.add(Finding("non-example-email", relative_path, line_number))

        for match in IPV4_RE.finditer(line):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            if address.is_private and not (address.is_loopback or address.is_link_local or address.is_reserved):
                findings.add(Finding("private-ip-address", relative_path, line_number))

        windows_user_match = WINDOWS_USER_PATH_RE.search(normalized_line)
        if windows_user_match and not looks_like_placeholder(windows_user_match.group(1)):
            findings.add(Finding("absolute-user-path", relative_path, line_number))
        unix_user_match = UNIX_USER_PATH_RE.search(line)
        if unix_user_match and not looks_like_placeholder(unix_user_match.group(1)):
            findings.add(Finding("absolute-user-path", relative_path, line_number))
        if WINDOWS_WORKSPACE_PATH_RE.search(normalized_line):
            findings.add(Finding("absolute-workspace-path", relative_path, line_number))

        if not relative_path.endswith(".lock") and any(
            luhn_valid(match.group(0)) for match in CARD_CANDIDATE_RE.finditer(line)
        ):
            findings.add(Finding("payment-card-number", relative_path, line_number))

    return sorted(findings)


def tracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8", errors="surrogateescape") for item in result.stdout.split(b"\0") if item]


def filesystem_paths(root: Path) -> list[str]:
    paths: list[str] = []
    for candidate in root.rglob("*"):
        relative = normalized_path(candidate.relative_to(root))
        if candidate.is_symlink():
            paths.append(relative)
            continue
        if candidate.is_file():
            paths.append(relative)
    return paths


def scan_paths(root: Path, paths: Iterable[str]) -> list[Finding]:
    findings: set[Finding] = set()
    for relative_path in sorted(set(normalized_path(path) for path in paths)):
        reason = prohibited_path_reason(relative_path)
        if reason:
            findings.add(Finding(reason, relative_path))
            continue

        absolute_path = root / Path(relative_path)
        if absolute_path.is_symlink():
            findings.add(Finding("symlink-requires-review", relative_path))
            continue
        try:
            size = absolute_path.stat().st_size
        except OSError:
            findings.add(Finding("unreadable-file", relative_path))
            continue
        if size > MAX_TEXT_BYTES:
            findings.add(Finding("oversized-file-requires-review", relative_path))
            continue
        if absolute_path.suffix.lower() not in TEXT_SUFFIXES and absolute_path.name not in {
            ".gitignore",
            ".dockerignore",
            ".env.example",
            ".env.sample",
            ".env.template",
        }:
            findings.add(Finding("binary-or-unknown-file-requires-review", relative_path))
            continue
        try:
            raw = absolute_path.read_bytes()
        except OSError:
            findings.add(Finding("unreadable-file", relative_path))
            continue
        if b"\0" in raw:
            findings.add(Finding("binary-file-requires-review", relative_path))
            continue
        text = raw.decode("utf-8", errors="replace")
        findings.update(scan_text(relative_path, text))
    return sorted(findings)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tracked", action="store_true", help="scan files tracked by Git (default)")
    mode.add_argument("--root", type=Path, help="scan every file beneath an exported source directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scan_root = (args.root or Path.cwd()).resolve()
    try:
        paths = filesystem_paths(scan_root) if args.root else tracked_paths(scan_root)
        findings = scan_paths(scan_root, paths)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"release guard could not complete: {type(exc).__name__}", file=sys.stderr)
        return 2

    if findings:
        print(f"release guard failed with {len(findings)} finding(s):", file=sys.stderr)
        for finding in findings:
            print(f"- {finding.safe_message()}", file=sys.stderr)
        return 1

    print(f"release guard passed: inspected {len(paths)} file(s); no prohibited content found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
