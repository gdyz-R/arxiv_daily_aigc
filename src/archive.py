"""Small helpers for public monthly archive paths.

Archive names are deliberately deterministic so a manual rerun replaces the
same edition instead of creating a second public file.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any


def safe_filename(value: str, fallback: str = "report", max_length: int = 80) -> str:
    """Return a readable filename without path separators or control chars."""

    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value)
    value = re.sub(r"[^\w\s.\-\u4e00-\u9fff]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"[\s_-]+", "-", value).strip(".-")
    return (value or fallback)[:max_length].rstrip(".-") or fallback


def report_slug(target_date: date, config: dict[str, Any]) -> str:
    project = config.get("project", {})
    title = (
        project.get("archive_title")
        or project.get("newspaper_name")
        or "AI-Research-Gazette"
    )
    max_length = int(config.get("render", {}).get("title_max_length", 80))
    return f"{target_date:%Y-%m-%d}-{safe_filename(title, max_length=max_length)}"


def report_paths(target_date: date, config: dict[str, Any]) -> tuple[Path, Path]:
    """Return JSON and HTML paths relative to the project root."""

    render = config["render"]
    month = f"{target_date:%Y-%m}"
    slug = report_slug(target_date, config)
    return (
        Path(render["json_dir"]) / month / f"{slug}.json",
        Path(render["output_dir"]) / month / f"{slug}.html",
    )


def relative_url(project_root: Path, source_path: Path, output_parent: Path) -> str:
    """Build a POSIX URL from an HTML file to a project-relative resource."""

    return Path(os.path.relpath(project_root / source_path, output_parent)).as_posix()
