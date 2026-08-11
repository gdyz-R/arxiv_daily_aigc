"""Render schema-v2 newspaper reports and legacy paper arrays to HTML."""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    from archive import relative_url, report_paths
    from config import PROJECT_ROOT, load_config
    from filter import focus_topic_for_date
except ImportError:  # pragma: no cover
    from .archive import relative_url, report_paths
    from .config import PROJECT_ROOT, load_config
    from .filter import focus_topic_for_date


LOGGER = logging.getLogger(__name__)

FIGURE_STATUS_LABELS = {
    "not_requested": "尚未请求论文图片。",
    "not_applicable": "该论文来源没有可关联的 arXiv HTML 图片。",
    "html_unavailable": "arXiv 尚未提供该论文的 HTML 版本。",
    "no_figure": "arXiv HTML 中未找到可用的 Figure 1 图片。",
    "request_failed": "请求 arXiv HTML 时发生网络错误。",
    "image_unavailable": "已定位 Figure 1，但图片地址未返回有效图像。",
    "image_request_failed": "下载 Figure 1 时发生网络错误。",
    "available": "Figure 1 已缓存到本地。",
}


def _parse_report_date(value: str | None, fallback_path: Path) -> date:
    if value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    try:
        match = re.match(r"(\d{4}[-_]\d{2}[-_]\d{2})", fallback_path.stem)
        if not match:
            raise ValueError
        return date.fromisoformat(match.group(1).replace("_", "-"))
    except ValueError:
        return date.today()


def normalize_report_payload(
    payload: Any, json_path: Path, config: dict[str, Any]
) -> dict[str, Any]:
    if isinstance(payload, dict) and payload.get("schema_version") == 2:
        payload.setdefault("report", {})
        payload.setdefault("papers", [])
        return payload
    if isinstance(payload, list):
        report_date = _parse_report_date(None, json_path)
        focus_topic = focus_topic_for_date(report_date, config)
        papers = sorted(
            payload,
            key=lambda paper: paper.get("overall_priority_score", 0),
            reverse=True,
        )
        for index, paper in enumerate(papers):
            paper.setdefault("primary_topic", focus_topic)
            paper.setdefault("content_tier", "major" if index < 2 else "brief")
            paper["is_hero"] = index == 0
        return {
            "schema_version": 2,
            "report": {
                "date": report_date.isoformat(),
                "newspaper_name": config["project"]["newspaper_name"],
                "subtitle": "Legacy archive · historical report compatibility view",
                "focus_topic": focus_topic,
                "focus_topic_name": config["topics"][focus_topic]["name"],
                "focus_topic_name_en": config["topics"][focus_topic]["name_en"],
                "actual_focus_count": len(papers),
                "actual_cross_topic_count": 0,
                "candidate_count": len(papers),
                "selected_count": len(papers),
                "distribution_note": "历史数组 JSON 兼容视图；保留原有论文排序。",
            },
            "papers": papers,
        }
    raise ValueError("Report JSON must be a schema-v2 object or a legacy paper array")


def _prepare_view(
    report: dict[str, Any], config: dict[str, Any], output_path: Path
) -> dict[str, Any]:
    papers = report.get("papers", [])
    report_meta = report.setdefault("report", {})
    report_date = _parse_report_date(report_meta.get("date"), Path("report.json"))
    report_meta.setdefault("date", report_date.isoformat())
    report_meta.setdefault("newspaper_name", config["project"]["newspaper_name"])
    report_meta.setdefault("subtitle", config["project"]["subtitle"])
    report_meta.setdefault("angle_name", "综合研究视角")
    report_meta.setdefault("angle_name_en", "General Research Perspective")
    report_meta.setdefault("search_query", "")
    topic_id = report_meta.get("focus_topic")
    if topic_id in config["topics"]:
        report_meta.setdefault("focus_topic_name", config["topics"][topic_id]["name"])
        report_meta.setdefault(
            "focus_topic_name_en", config["topics"][topic_id]["name_en"]
        )
    hero = next((paper for paper in papers if paper.get("is_hero")), None)
    if not hero:
        hero = next(
            (paper for paper in papers if paper.get("content_tier") == "major"), None
        )
    if not hero and papers:
        hero = papers[0]
        hero.setdefault("content_tier", "major")
        hero["is_hero"] = True
    major_features = [
        paper
        for paper in papers
        if paper is not hero and paper.get("content_tier") == "major"
    ]
    briefs = [
        paper for paper in papers if paper is not hero and paper not in major_features
    ]
    for paper in papers:
        topic = config["topics"].get(paper.get("primary_topic"), {})
        paper.setdefault("topic_name", topic.get("name", "AI Research"))
        paper.setdefault("topic_name_en", topic.get("name_en", "AI Research"))
        paper.setdefault("newspaper_title", paper.get("title", "Untitled"))
        paper.setdefault(
            "dek",
            paper.get("tldr_zh") or paper.get("tldr") or paper.get("summary", ""),
        )
        paper.setdefault("citation_count", 0)
        paper.setdefault("venue_tags", [])
        paper.setdefault("contribution_tags", [])
        paper.setdefault("brief_points", [])
        paper.setdefault("core_innovations", [])
        paper.setdefault("background_and_pain", "")
        paper.setdefault("experimental_findings", "")
        paper.setdefault("figure_explanation", "")
        paper.setdefault(
            "figure_status", "available" if paper.get("figure_url") else "not_requested"
        )
        if paper.get("figure_status") != "available":
            paper["figure_url"] = None
        paper.setdefault(
            "figure_status_label",
            FIGURE_STATUS_LABELS.get(
                paper["figure_status"], "当前没有可用于版面的论文图片。"
            ),
        )
        paper.setdefault("abstract_url", paper.get("url"))
        paper.setdefault("pdf_url", paper.get("url"))
        cache_path = paper.get("figure_cache_path")
        if cache_path and paper.get("figure_status") == "available":
            absolute_cache = PROJECT_ROOT / str(cache_path)
            if absolute_cache.is_file():
                paper["figure_url"] = relative_url(
                    PROJECT_ROOT, Path(str(cache_path)), output_path.parent
                )
            else:
                paper["figure_url"] = None
    return {
        "report": report_meta,
        "papers": papers,
        "hero": hero,
        "major_features": major_features,
        "briefs": briefs,
        "report_date": report_date,
        "generation_time": datetime.now(timezone.utc),
        "topics": config["topics"],
        "title": f"{report_meta['newspaper_name']} — {report_date.isoformat()}",
    }


def render_report(
    report: dict[str, Any],
    output_path: str | Path,
    config: dict[str, Any] | None = None,
    template_path: str | Path | None = None,
) -> Path:
    config = config or load_config()
    report = deepcopy(report)
    template_file = (
        Path(template_path)
        if template_path
        else PROJECT_ROOT / "templates" / config["render"]["template"]
    )
    if not template_file.is_absolute():
        template_file = PROJECT_ROOT / template_file
    env = Environment(
        loader=FileSystemLoader(str(template_file.parent)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    target = Path(output_path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    stylesheet_file = template_file.parent / config["render"].get(
        "stylesheet", "styles.css"
    )
    public_stylesheet = Path(
        config["render"].get("public_stylesheet", "assets/report.css")
    )
    public_stylesheet_path = PROJECT_ROOT / public_stylesheet
    public_stylesheet_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(stylesheet_file, public_stylesheet_path)
    view = _prepare_view(report, config, target)
    view["stylesheet_url"] = relative_url(
        PROJECT_ROOT, public_stylesheet, target.parent
    )
    view["image_max_height"] = int(config["render"].get("image_max_height", 450))
    html = env.get_template(template_file.name).render(**view)
    target.write_text(html, encoding="utf-8")
    try:
        display_target = target.relative_to(PROJECT_ROOT)
    except ValueError:
        display_target = Path(target.name)
    LOGGER.info("Rendered newspaper: %s", display_target)
    return target


def render_json_file(
    json_file_path: str | Path,
    output_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> Path:
    config = config or load_config()
    json_path = Path(json_file_path)
    if not json_path.is_absolute():
        json_path = PROJECT_ROOT / json_path
    report = normalize_report_payload(
        json.loads(json_path.read_text(encoding="utf-8")), json_path, config
    )
    target_path: str | Path
    if output_path is None:
        report_date = _parse_report_date(
            report.get("report", {}).get("date"), json_path
        )
        _, html_path = report_paths(report_date, config)
        target_path = PROJECT_ROOT / html_path
    else:
        target_path = output_path
    return render_report(report, target_path, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render an AI newspaper JSON report")
    parser.add_argument("--input", required=True, help="Input JSON report")
    parser.add_argument("--output", help="Output HTML path")
    parser.add_argument("--config", help="Alternative config.yaml path")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    render_json_file(args.input, args.output, load_config(args.config))


if __name__ == "__main__":
    main()
