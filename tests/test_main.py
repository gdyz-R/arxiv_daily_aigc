from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config import load_config
from src.main import _report_paths, _update_reports_index, generate_daily_report
from src.memory import MemoryReadResult, MemoryWriteResult
from src.scheduler import ScheduleDecision


class MainArchiveTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_report_paths_use_month_and_title(self):
        with patch("src.main.PROJECT_ROOT", Path("C:/project")):
            json_path, html_path = _report_paths(date(2026, 8, 10), self.config)
        self.assertEqual(
            json_path.as_posix(),
            "C:/project/daily_json/2026-08/2026-08-10-AI研究日报.json",
        )
        self.assertEqual(
            html_path.as_posix(),
            "C:/project/daily_html/2026-08/2026-08-10-AI研究日报.html",
        )

    def test_reports_index_contains_new_and_legacy_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html_root = root / "daily_html"
            (html_root / "2026-08").mkdir(parents=True)
            (html_root / "2025_10_28.html").write_text("old", encoding="utf-8")
            (html_root / "2026-08" / "2026-08-10-AI研究日报.html").write_text(
                "new", encoding="utf-8"
            )
            with patch("src.main.PROJECT_ROOT", root):
                _update_reports_index(self.config)
            reports = json.loads((root / "reports.json").read_text(encoding="utf-8"))
        self.assertEqual(
            reports,
            ["2026-08/2026-08-10-AI研究日报.html", "2025_10_28.html"],
        )

    def test_empty_existing_report_is_regenerated_and_not_republished(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config()
            config["_meta"]["project_root"] = directory
            with patch("src.main.PROJECT_ROOT", root):
                json_path, html_path = _report_paths(date(2026, 8, 11), config)
                json_path.parent.mkdir(parents=True)
                empty_report = {
                    "schema_version": 2,
                    "report": {"date": "2026-08-11"},
                    "papers": [],
                }
                json_path.write_text(json.dumps(empty_report), encoding="utf-8")
                original_json = json_path.read_text(encoding="utf-8")
                diagnostics = {
                    "arxiv": {"status": "failed", "result_count": 0},
                    "openalex": {"status": "empty", "result_count": 0},
                    "neurips": {"status": "empty", "result_count": 0},
                }
                with (
                    patch(
                        "src.main.crawl_papers_with_diagnostics",
                        return_value=([], diagnostics),
                    ),
                    patch("src.main.render_report") as render_report,
                ):
                    with self.assertRaisesRegex(RuntimeError, "Refusing to publish"):
                        generate_daily_report(date(2026, 8, 11), config)
                self.assertEqual(json_path.read_text(encoding="utf-8"), original_json)
                self.assertFalse(html_path.exists())
                render_report.assert_not_called()

    def test_new_empty_crawl_does_not_create_report_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config()
            config["_meta"]["project_root"] = directory
            diagnostics = {
                "arxiv": {"status": "failed", "result_count": 0},
                "openalex": {"status": "empty", "result_count": 0},
                "neurips": {"status": "empty", "result_count": 0},
            }
            with (
                patch("src.main.PROJECT_ROOT", root),
                patch(
                    "src.main.crawl_papers_with_diagnostics",
                    return_value=([], diagnostics),
                ),
                patch("src.main.render_report") as render_report,
            ):
                json_path, html_path = _report_paths(date(2026, 8, 11), config)
                with self.assertRaisesRegex(RuntimeError, "at crawl"):
                    generate_daily_report(date(2026, 8, 11), config, force=True)
            self.assertFalse(json_path.exists())
            self.assertFalse(html_path.exists())
            render_report.assert_not_called()

    def test_full_pipeline_keeps_memory_payload_out_of_public_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config()
            config["_meta"]["project_root"] = directory
            paper = {
                "paper_id": "paper:1",
                "title": "Private Memory Test",
                "summary": "We propose a system and report experiments.",
                "authors": ["A"],
                "primary_topic": "kv_cache_memory",
                "topic_scores": {"kv_cache_memory": 9},
                "novelty_score": 9,
                "potential_impact_score": 9,
                "clarity_score": 8,
                "is_relevant": True,
                "citation_count": 0,
                "venue_tags": ["ICLR"],
                "well_known": True,
                "well_known_reasons": ["top_venue:ICLR"],
                "historical_anchor": True,
                "categories": ["cs.CL"],
                "published_date": "2022-01-01T00:00:00+00:00",
                "abstract_url": "https://example.com/abs",
                "pdf_url": "https://example.com/pdf",
                "content_tier": "major",
                "is_hero": True,
                "newspaper_title": "测试日报",
                "dek": "导语",
                "background_and_pain": "背景",
                "core_innovations": ["创新"],
                "experimental_findings": "结论",
                "brief_points": [],
                "contribution_tags": ["KV Cache"],
            }
            decision = ScheduleDecision(
                topic_id="kv_cache_memory",
                topic_name="KV Cache 与推理内存系统",
                topic_name_en="KV Cache & Inference Memory",
                angle_id="systems_tradeoffs",
                angle_name="系统权衡与成本结构",
                angle_name_en="System Trade-offs & Cost Structure",
                angle_instruction="分析成本结构",
                search_query="private query",
                days_unselected=12,
                cooldown_readiness=0.8,
                selection_reason="cooldown_weighted_rotation",
            )
            memory_client = MagicMock()
            memory_client.write.return_value = MemoryWriteResult("updated", True)
            private_summary = "PRIVATE_LEDGER_SUMMARY_MUST_NOT_PUBLISH"
            read = MemoryReadResult(
                {
                    "schema_version": 1,
                    "updated_at": None,
                    "concepts": {
                        "kv_cache": {
                            "name": "KV Cache",
                            "status": "learning",
                            "mastery_level": 0.5,
                            "mastery_summary": private_summary,
                            "source_reports": [],
                        }
                    },
                },
                "available",
                True,
            )
            payload_summary = "PRIVATE_PAYLOAD_MUST_NOT_PUBLISH"
            with (
                patch("src.main.PROJECT_ROOT", root),
                patch("src.main.schedule_daily_focus", return_value=decision),
                patch(
                    "src.main._read_private_memory", return_value=(memory_client, read)
                ),
                patch(
                    "src.main.crawl_papers_with_diagnostics",
                    return_value=(
                        [paper],
                        {"arxiv": {"status": "available", "result_count": 1}},
                    ),
                ),
                patch("src.main.coarse_classify_papers", return_value=[paper]),
                patch("src.main.prefilter_coarse_candidates", return_value=[paper]),
                patch(
                    "src.main.rank_candidate_shortlist",
                    return_value=([paper], {"candidate_count": 1}),
                ),
                patch(
                    "src.main.generate_memory_aware_edition",
                    return_value=(
                        [paper],
                        {
                            "focus_topic": "kv_cache_memory",
                            "actual_focus_count": 1,
                            "actual_cross_topic_count": 0,
                            "candidate_count": 1,
                            "selected_count": 1,
                            "distribution_note": "动态精选。",
                        },
                        {
                            "concept_updates": [
                                {
                                    "concept_id": "kv_cache",
                                    "name": "KV Cache",
                                    "status": "mastered",
                                    "mastery_level": 0.9,
                                    "mastery_summary": payload_summary,
                                }
                            ]
                        },
                    ),
                ),
                patch("src.main.enrich_selected_figures", return_value=[paper]),
                patch("src.main.generate_figure_explanations", return_value=[paper]),
                patch("src.main.render_report") as render_report,
            ):
                json_path, _ = generate_daily_report(
                    date(2026, 8, 11), config, force=True
                )
            public_text = json_path.read_text(encoding="utf-8")
            public_payload = json.loads(public_text)
        self.assertNotIn(private_summary, public_text)
        self.assertNotIn(payload_summary, public_text)
        self.assertNotIn("memory_payload", public_text)
        self.assertEqual(public_payload["report"]["memory_write_status"], "updated")
        memory_client.write.assert_called_once()
        render_report.assert_called_once()

    def test_prominence_noncompliant_report_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config()
            config["_meta"]["project_root"] = directory
            paper = {
                "paper_id": "paper:recent",
                "title": "Recent Paper",
                "summary": "We propose a recent method.",
                "primary_topic": "kv_cache_memory",
                "candidate_topics": ["kv_cache_memory"],
                "published_date": "2026-08-10T00:00:00+00:00",
                "well_known": False,
                "historical_anchor": False,
            }
            decision = ScheduleDecision(
                topic_id="kv_cache_memory",
                topic_name="KV Cache 与推理内存系统",
                topic_name_en="KV Cache & Inference Memory",
                angle_id="systems_tradeoffs",
                angle_name="系统权衡与成本结构",
                angle_name_en="System Trade-offs & Cost Structure",
                angle_instruction="分析成本结构",
                search_query="query",
                days_unselected=5,
                cooldown_readiness=0.5,
                selection_reason="cooldown_weighted_rotation",
            )
            with (
                patch("src.main.PROJECT_ROOT", root),
                patch("src.main.schedule_daily_focus", return_value=decision),
                patch(
                    "src.main._read_private_memory",
                    return_value=(None, MemoryReadResult({"concepts": {}}, "empty")),
                ),
                patch(
                    "src.main.crawl_papers_with_diagnostics",
                    return_value=(
                        [paper],
                        {"arxiv": {"status": "available", "result_count": 1}},
                    ),
                ),
                patch("src.main.prefilter_coarse_candidates", return_value=[paper]),
                patch("src.main.coarse_classify_papers", return_value=[paper]),
                patch(
                    "src.main.rank_candidate_shortlist",
                    return_value=([paper], {"candidate_count": 1}),
                ),
                patch(
                    "src.main.generate_memory_aware_edition",
                    return_value=(
                        [paper],
                        {
                            "candidate_count": 1,
                            "selected_count": 1,
                            "distribution_note": "测试。",
                        },
                        {"concept_updates": []},
                    ),
                ),
                patch("src.main.render_report") as render_report,
            ):
                json_path, html_path = _report_paths(date(2026, 8, 11), config)
                with self.assertRaisesRegex(RuntimeError, "prominence-noncompliant"):
                    generate_daily_report(date(2026, 8, 11), config, force=True)
            self.assertFalse(json_path.exists())
            self.assertFalse(html_path.exists())
            render_report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
