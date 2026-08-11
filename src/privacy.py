"""Checks and sanitizes files that are safe to publish on GitHub Pages."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {
    "api_key",
    "api_key_env",
    "authorization",
    "headers",
    "password",
    "secret",
    "token",
    "gist_id",
    "gist_token",
    "memory_payload",
    "concept_ledger",
    "concept_memory",
}
PUBLIC_DETAIL_FIELDS = {"figure_status_detail"}
SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._~+/=-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    re.IGNORECASE,
)
WINDOWS_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]:\\(?:Users|Documents and Settings|ProgramData|Windows|Program Files(?: \(x86\))?|Temp)\\[^\n\r\"']+"
)
UNIX_HOME_PATTERN = re.compile(
    r"(?<![A-Za-z0-9:])(?:/home/[^/\s\"']+|/Users/[^/\s\"']+)(?:/[^\s\"']*)?"
)
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".json",
    ".md",
    ".svg",
    ".txt",
    ".xml",
    ".yml",
    ".yaml",
}
SECRET_ENV_NAMES = (
    "DEEPSEEK_API_KEY",
    "DASUAPI_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
    "GIST_ID",
    "GIST_TOKEN",
)


def _known_secret_values() -> list[str]:
    return [
        value
        for name in SECRET_ENV_NAMES
        if (value := os.getenv(name)) and len(value) >= 8
    ]


def _redact_text(value: str, project_root: str | None = None) -> str:
    result = SECRET_PATTERN.sub("[REDACTED]", value)
    for secret in _known_secret_values():
        result = result.replace(secret, "[REDACTED]")
    if project_root:
        result = result.replace(project_root, "[PROJECT_ROOT]")
        result = result.replace(project_root.replace("\\", "/"), "[PROJECT_ROOT]")
    result = WINDOWS_PATH_PATTERN.sub("[LOCAL_PATH]", result)
    return UNIX_HOME_PATTERN.sub("[LOCAL_PATH]", result)


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value


def sanitize_public_value(value: Any, project_root: str | None = None) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                continue
            if str(key) == "_meta":
                continue
            if str(key) in PUBLIC_DETAIL_FIELDS:
                cleaned[key] = _redact_text(str(item), project_root)[:160]
                continue
            cleaned[key] = sanitize_public_value(item, project_root)
        return cleaned
    if isinstance(value, list):
        return [sanitize_public_value(item, project_root) for item in value]
    if isinstance(value, str):
        return _redact_text(value, project_root)
    return value


def sanitize_public_report(
    report: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    root = str(config.get("_meta", {}).get("project_root", "")) or None
    return sanitize_public_value(report, root)


def scan_paths(paths: Iterable[Path]) -> list[str]:
    findings: list[str] = []
    known_secrets = _known_secret_values()
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        searchable_values = [text]
        if path.suffix.lower() == ".json":
            try:
                searchable_values = list(_string_values(json.loads(text)))
            except (json.JSONDecodeError, TypeError):
                pass
        searchable_text = "\n".join(searchable_values)
        if SECRET_PATTERN.search(searchable_text):
            findings.append(f"secret-like value in {path}")
        if any(secret in searchable_text for secret in known_secrets):
            findings.append(f"configured secret value in {path}")
        if WINDOWS_PATH_PATTERN.search(searchable_text) or UNIX_HOME_PATTERN.search(
            searchable_text
        ):
            findings.append(f"local absolute path in {path}")
        if re.search(
            r"(?:(?:DEEPSEEK|DASUAPI|OPENROUTER|SEMANTIC_SCHOLAR)_API_KEY|GIST_ID|GIST_TOKEN)\s*=",
            text,
        ):
            findings.append(f"API key assignment in {path}")
    return findings


def _iter_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
    elif path.is_dir():
        yield from (item for item in path.rglob("*") if item.is_file())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check public report files for secrets"
    )
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    findings = scan_paths(item for path in args.paths for item in _iter_files(path))
    if findings:
        raise SystemExit("Public output privacy check failed:\n" + "\n".join(findings))
    print("Public output privacy check passed.")


if __name__ == "__main__":
    main()
