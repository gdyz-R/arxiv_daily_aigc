from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.config import load_config
from src.main import _report_paths, _update_reports_index


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


if __name__ == "__main__":
    unittest.main()
