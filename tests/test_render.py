from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config import load_config
from src.render import normalize_report_payload, render_report


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.fixture = Path(__file__).parent / "fixtures" / "report_v2.json"

    def test_schema_v2_renders_figure_and_labels(self):
        report = json.loads(self.fixture.read_text(encoding="utf-8"))
        report["report"].update(
            {
                "angle_name": "系统权衡与成本结构",
                "angle_name_en": "System Trade-offs & Cost Structure",
                "search_query": 'cat:cs.CL AND ti:"agent memory"',
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            output = render_report(report, Path(directory) / "report.html", self.config)
            html = output.read_text(encoding="utf-8")
        self.assertIn('src="../assets/figures/demo/agent-memory-figure1.svg"', html)
        self.assertIn("NeurIPS", html)
        self.assertIn("Oral", html)
        self.assertIn("重大突破", html)
        self.assertIn("briefs-grid", html)
        self.assertIn("feature no-figure", html)
        self.assertNotIn("图片说明：", html)
        self.assertNotIn("figure-placeholder", html)
        self.assertIn('assets/report.css"', html)
        self.assertIn("问题背景", html)
        self.assertIn("方法与贡献", html)
        self.assertIn("实验与结论", html)
        self.assertIn("今日视角", html)
        self.assertIn("系统权衡与成本结构", html)
        self.assertIn("Today's Search Query", html)
        self.assertIn("--figure-max-height: 450px", html)

    def test_monthly_output_uses_month_relative_assets(self):
        report = json.loads(self.fixture.read_text(encoding="utf-8"))
        html_root = Path("daily_html")
        html_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=html_root) as directory:
            output = render_report(
                report,
                Path(directory) / "2026-08-10-AI研究日报.html",
                self.config,
            )
            html = output.read_text(encoding="utf-8")
            self.assertIn('href="../../assets/report.css"', html)

    def test_legacy_array_is_not_filtered_away(self):
        legacy = [
            {"title": "Legacy A", "summary": "Old report", "overall_priority_score": 9},
            {"title": "Legacy B", "summary": "Old report", "overall_priority_score": 8},
        ]
        normalized = normalize_report_payload(
            legacy, Path("2025-04-23.json"), self.config
        )
        self.assertEqual(len(normalized["papers"]), 2)
        self.assertTrue(normalized["papers"][0]["is_hero"])

    def test_non_available_figure_status_never_renders_stale_url(self):
        report = json.loads(self.fixture.read_text(encoding="utf-8"))
        report["papers"][0]["figure_status"] = "image_request_failed"
        with tempfile.TemporaryDirectory() as directory:
            output = render_report(report, Path(directory) / "report.html", self.config)
            html = output.read_text(encoding="utf-8")
        self.assertNotIn("agent-memory-figure1.svg", html)
        self.assertIn('class="hero no-figure"', html)

    def test_css_uses_natural_image_size_without_forced_aspect_ratio(self):
        report = json.loads(self.fixture.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            render_report(report, Path(directory) / "report.html", self.config)
            css = Path("assets/report.css").read_text(encoding="utf-8")
        self.assertIn("max-height: var(--figure-max-height, 450px)", css)
        self.assertIn(".paper-figure", css)
        self.assertNotIn("aspect-ratio:", css)
        self.assertNotIn("height: 100%", css)

    def test_brief_grid_redistributes_incomplete_last_rows(self):
        report = json.loads(self.fixture.read_text(encoding="utf-8"))
        while len(report["papers"]) < 6:
            clone = dict(report["papers"][-1])
            clone["paper_id"] = f"brief:{len(report['papers'])}"
            clone["title"] = f"Brief {len(report['papers'])}"
            clone["newspaper_title"] = clone["title"]
            clone["content_tier"] = "brief"
            clone["is_hero"] = False
            report["papers"].append(clone)
        with tempfile.TemporaryDirectory() as directory:
            render_report(report, Path(directory) / "report.html", self.config)
            css = Path("assets/report.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: repeat(6", css)
        self.assertIn(".brief:nth-last-child(1):nth-child(3n+1)", css)
        self.assertIn("grid-column: 1 / -1", css)
        self.assertIn(".brief:last-child:nth-child(odd)", css)
        self.assertIn("border-left: 0;\n        padding-left: 0", css)


if __name__ == "__main__":
    unittest.main()
