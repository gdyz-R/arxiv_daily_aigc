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


if __name__ == "__main__":
    unittest.main()
