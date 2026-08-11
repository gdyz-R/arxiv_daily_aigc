"""Command-line orchestration for the Daily AI Research Gazette."""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from archive import report_paths
    from config import PROJECT_ROOT, load_config
    from crawl import crawl_papers_with_diagnostics, enrich_selected_figures
    from filter import (
        coarse_classify_papers,
        generate_editorial_content,
        select_daily_papers,
    )
    from privacy import sanitize_public_report
    from render import normalize_report_payload, render_report
except ImportError:  # pragma: no cover
    from .archive import report_paths
    from .config import PROJECT_ROOT, load_config
    from .crawl import crawl_papers_with_diagnostics, enrich_selected_figures
    from .filter import (
        coarse_classify_papers,
        generate_editorial_content,
        select_daily_papers,
    )
    from .privacy import sanitize_public_report
    from .render import normalize_report_payload, render_report

LOGGER = logging.getLogger(__name__)


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _report_paths(target_date: date, config: dict[str, Any]) -> tuple[Path, Path]:
    json_path, html_path = report_paths(target_date, config)
    return PROJECT_ROOT / json_path, PROJECT_ROOT / html_path


def _write_report(report: dict[str, Any], path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    public_report = sanitize_public_report(report, config)
    path.write_text(
        json.dumps(public_report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _update_reports_index(config: dict[str, Any]) -> None:
    html_dir = PROJECT_ROOT / config["render"]["output_dir"]
    dated_files: list[tuple[str, str]] = []
    for item in html_dir.rglob("*.html"):
        match = re.match(r"(?P<date>\d{4}[-_]\d{2}[-_]\d{2})", item.name)
        if not match:
            continue
        sortable_date = match.group("date").replace("_", "-")
        dated_files.append((sortable_date, item.relative_to(html_dir).as_posix()))
    files = [path for _, path in sorted(dated_files, reverse=True)]
    (PROJECT_ROOT / "reports.json").write_text(
        json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _empty_report_error(
    target_date: date, source_diagnostics: dict[str, Any], stage: str
) -> RuntimeError:
    source_summary = ", ".join(
        f"{name}={details.get('status', 'unknown')}:{details.get('result_count', 0)}"
        for name, details in source_diagnostics.items()
        if isinstance(details, dict) and name != "semantic_scholar"
    )
    return RuntimeError(
        f"Refusing to publish an empty report for {target_date} at {stage}; "
        f"source diagnostics: {source_summary or 'unavailable'}"
    )


def generate_daily_report(
    target_date: date,
    config: dict[str, Any],
    *,
    force: bool = False,
    offline_render: bool = False,
    allow_empty: bool = False,
) -> tuple[Path, Path]:
    json_path, html_path = _report_paths(target_date, config)
    if json_path.exists() and not force:
        existing_report = normalize_report_payload(
            json.loads(json_path.read_text(encoding="utf-8")), json_path, config
        )
        if offline_render or existing_report.get("papers") or allow_empty:
            LOGGER.info(
                "Using existing JSON report: %s", json_path.relative_to(PROJECT_ROOT)
            )
            report = existing_report
        else:
            LOGGER.warning(
                "Existing report is empty; regenerating instead of reusing: %s",
                json_path.relative_to(PROJECT_ROOT),
            )
            force = True
    elif offline_render:
        raise FileNotFoundError(
            f"Offline render requested but report does not exist: {json_path}"
        )
    if force or not json_path.exists():
        LOGGER.info("Crawling papers for %s", target_date)
        candidates, source_diagnostics = crawl_papers_with_diagnostics(
            target_date, config
        )
        LOGGER.info("Crawled %s candidates", len(candidates))
        LOGGER.info(
            "Source diagnostics: %s",
            json.dumps(source_diagnostics, ensure_ascii=False, sort_keys=True),
        )
        if not candidates and not allow_empty:
            raise _empty_report_error(target_date, source_diagnostics, "crawl")
        candidates = coarse_classify_papers(candidates, config)
        selected, meta = select_daily_papers(candidates, target_date, config)
        LOGGER.info("Selected %s papers (%s)", len(selected), meta["distribution_note"])
        if not selected and not allow_empty:
            raise _empty_report_error(target_date, source_diagnostics, "selection")
        selected = enrich_selected_figures(selected, config, target_date)
        selected = generate_editorial_content(selected, config)
        topic_id = meta["focus_topic"]
        report = {
            "schema_version": 2,
            "report": {
                "date": target_date.isoformat(),
                "newspaper_name": config["project"]["newspaper_name"],
                "subtitle": config["project"]["subtitle"],
                "focus_topic": topic_id,
                "focus_topic_name": config["topics"][topic_id]["name"],
                "focus_topic_name_en": config["topics"][topic_id]["name_en"],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_diagnostics": source_diagnostics,
                **meta,
            },
            "papers": selected,
        }
        report = sanitize_public_report(report, config)
        _write_report(report, json_path, config)
    render_report(report, html_path, config)
    _update_reports_index(config)
    return json_path, html_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the personal Daily AI Research Gazette"
    )
    parser.add_argument("--date", help="Report date YYYY-MM-DD; defaults to today")
    parser.add_argument("--config", help="Alternative config.yaml path")
    parser.add_argument(
        "--force", action="store_true", help="Regenerate even when JSON exists"
    )
    parser.add_argument(
        "--offline-render",
        action="store_true",
        help="Only render an existing JSON; never call APIs",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Permit an empty report (disabled by default to protect public output)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    config = load_config(args.config)
    try:
        target_date = (
            datetime.strptime(args.date, "%Y-%m-%d").date()
            if args.date
            else datetime.now(ZoneInfo(config["project"]["timezone"])).date()
        )
    except (ValueError, KeyError) as exc:
        parser.error(f"Invalid --date; expected YYYY-MM-DD: {exc}")
    json_path, html_path = generate_daily_report(
        target_date,
        config,
        force=args.force,
        offline_render=args.offline_render,
        allow_empty=args.allow_empty,
    )
    LOGGER.info(
        "Done: %s and %s",
        json_path.relative_to(PROJECT_ROOT),
        html_path.relative_to(PROJECT_ROOT),
    )


if __name__ == "__main__":
    main()
