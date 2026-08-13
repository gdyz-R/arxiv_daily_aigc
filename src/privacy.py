"""Sanitizes report data before it is written to public files."""

from __future__ import annotations

import os
import re
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
SECRET_ENV_NAMES = (
    "COARSE_LLM_API_KEY",
    "EDITORIAL_LLM_API_KEY",
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
