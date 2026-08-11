from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.config import load_config
from src.main import _report_paths, _update_reports_index, generate_daily_report


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


if __name__ == "__main__":
    unittest.main()
