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
    from config import PROJECT_ROOT, load_config, secret_from
    from crawl import crawl_papers_with_diagnostics, enrich_selected_figures
    from filter import (
        coarse_classify_papers,
        generate_figure_explanations,
        generate_memory_aware_edition,
        prefilter_coarse_candidates,
        rank_candidate_shortlist,
    )
    from memory import (
        GitHubGistMemoryClient,
        MemoryReadResult,
        MemoryWriteResult,
        empty_ledger,
        merge_concept_updates,
        relevant_memory_context,
    )
    from privacy import sanitize_public_report
    from prominence import prominence_policy_errors, prominence_summary
    from render import normalize_report_payload, render_report
    from scheduler import schedule_daily_focus, topic_query_order
except ImportError:  # pragma: no cover
    from .archive import report_paths
    from .config import PROJECT_ROOT, load_config, secret_from
    from .crawl import crawl_papers_with_diagnostics, enrich_selected_figures
    from .filter import (
        coarse_classify_papers,
        generate_figure_explanations,
        generate_memory_aware_edition,
        prefilter_coarse_candidates,
        rank_candidate_shortlist,
    )
    from .memory import (
        GitHubGistMemoryClient,
        MemoryReadResult,
        MemoryWriteResult,
        empty_ledger,
        merge_concept_updates,
        relevant_memory_context,
    )
    from .privacy import sanitize_public_report
    from .prominence import prominence_policy_errors, prominence_summary
    from .render import normalize_report_payload, render_report
    from .scheduler import schedule_daily_focus, topic_query_order

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


def _read_private_memory(
    config: dict[str, Any],
) -> tuple[GitHubGistMemoryClient | None, MemoryReadResult]:
    section = config.get("memory", {})
    if section.get("provider") != "github_gist":
        return None, MemoryReadResult(empty_ledger(), "empty_disabled")
    client = GitHubGistMemoryClient(section)
    return client, client.read()


def _update_private_memory(
    client: GitHubGistMemoryClient | None,
    ledger: dict[str, Any],
    memory_payload: dict[str, Any],
    target_date: date,
) -> tuple[MemoryWriteResult, int]:
    merged, update_count = merge_concept_updates(ledger, memory_payload, target_date)
    if update_count == 0:
        return MemoryWriteResult("skipped_no_updates"), 0
    if client is None:
        return MemoryWriteResult("skipped_disabled"), update_count
    return client.write(merged), update_count


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
        decision = schedule_daily_focus(target_date, config, project_root=PROJECT_ROOT)
        schedule = decision.as_dict()
        LOGGER.info(
            "Scheduled topic=%s angle=%s days_unselected=%s readiness=%s",
            decision.topic_id,
            decision.angle_id,
            decision.days_unselected,
            decision.cooldown_readiness,
        )
        memory_client, memory_read = _read_private_memory(config)
        concept_ids = [
            str(item)
            for item in config["topics"][decision.topic_id].get("concepts", [])
        ]
        memory_context = relevant_memory_context(
            memory_read.ledger,
            concept_ids,
            limit=int(config["memory"].get("context_concept_limit", 12)),
        )
        LOGGER.info(
            "Memory mode=%s relevant_concepts=%s",
            memory_read.status,
            len(memory_context),
        )
        LOGGER.info("Crawling papers for %s", target_date)
        candidates, source_diagnostics = crawl_papers_with_diagnostics(
            target_date,
            config,
            topic_order=topic_query_order(decision, config),
            enrich_semantic_scholar=bool(
                secret_from(config["sources"]["semantic_scholar"])
            ),
        )
        LOGGER.info("Crawled %s candidates", len(candidates))
        LOGGER.info(
            "Source diagnostics: %s",
            json.dumps(source_diagnostics, ensure_ascii=False, sort_keys=True),
        )
        if not candidates and not allow_empty:
            raise _empty_report_error(target_date, source_diagnostics, "crawl")
        candidates = prefilter_coarse_candidates(
            candidates, target_date, config, decision.topic_id
        )
        candidates = coarse_classify_papers(candidates, config)
        shortlist, shortlist_meta = rank_candidate_shortlist(
            candidates, target_date, config, decision.topic_id
        )
        LOGGER.info(
            "Prepared %s candidate papers for memory-aware editing", len(shortlist)
        )
        if not shortlist and not allow_empty:
            raise _empty_report_error(target_date, source_diagnostics, "selection")
        selected, meta, memory_payload = generate_memory_aware_edition(
            shortlist,
            target_date,
            config,
            schedule,
            memory_context,
        )
        meta["source_candidate_count"] = shortlist_meta.get("candidate_count", 0)
        meta["shortlist_count"] = len(shortlist)
        LOGGER.info("Selected %s papers (%s)", len(selected), meta["distribution_note"])
        if not selected and not allow_empty:
            raise _empty_report_error(target_date, source_diagnostics, "editorial")
        if selected:
            prominence_errors = prominence_policy_errors(
                selected, decision.topic_id, config
            )
            if prominence_errors:
                raise RuntimeError(
                    f"Refusing to publish a prominence-noncompliant report for "
                    f"{target_date}: {'; '.join(prominence_errors)}"
                )
            meta.update(prominence_summary(selected, decision.topic_id))
        selected = enrich_selected_figures(selected, config, target_date)
        selected = generate_figure_explanations(selected, config)
        memory_write, concept_update_count = _update_private_memory(
            memory_client,
            memory_read.ledger,
            memory_payload,
            target_date,
        )
        LOGGER.info(
            "Memory write=%s validated_updates=%s",
            memory_write.status,
            concept_update_count,
        )
        topic_id = decision.topic_id
        report = {
            "schema_version": 2,
            "report": {
                "date": target_date.isoformat(),
                "newspaper_name": config["project"]["newspaper_name"],
                "subtitle": config["project"]["subtitle"],
                "focus_topic": topic_id,
                "focus_topic_name": config["topics"][topic_id]["name"],
                "focus_topic_name_en": config["topics"][topic_id]["name_en"],
                "search_query": decision.search_query,
                "angle_id": decision.angle_id,
                "angle_name": decision.angle_name,
                "angle_name_en": decision.angle_name_en,
                "schedule_days_unselected": decision.days_unselected,
                "schedule_cooldown_readiness": decision.cooldown_readiness,
                "schedule_selection_reason": decision.selection_reason,
                "memory_read_status": memory_read.status,
                "memory_write_status": memory_write.status,
                "memory_concept_update_count": concept_update_count,
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
