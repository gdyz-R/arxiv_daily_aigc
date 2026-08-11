from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.archive import report_paths, safe_filename
from src.config import load_config
from src.privacy import sanitize_public_report, scan_paths


class ArchivePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_monthly_archive_paths_include_date_and_title(self):
        json_path, html_path = report_paths(date(2026, 8, 10), self.config)
        expected = "2026-08-10-AI研究日报"
        self.assertEqual(json_path.as_posix(), f"daily_json/2026-08/{expected}.json")
        self.assertEqual(html_path.as_posix(), f"daily_html/2026-08/{expected}.html")
        self.assertEqual(safe_filename("a/b: c?"), "a-b-c")

    def test_public_report_redacts_secrets_and_local_paths(self):
        report = {
            "api_key": "sk-secretsecretsecretsecret",
            "path": r"C:\Users\Private\file.txt",
            "nested": {"authorization": "Bearer abcdefghijklmnop", "ok": "public"},
        }
        cleaned = sanitize_public_report(report, self.config)
        self.assertNotIn("api_key", cleaned)
        self.assertNotIn("authorization", cleaned["nested"])
        self.assertEqual(cleaned["nested"]["ok"], "public")
        self.assertNotIn(r"C:\Users", cleaned["path"])

    def test_privacy_scanner_detects_configured_gist_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text('{"debug":"gist-identifier-12345"}', encoding="utf-8")
            with patch.dict(os.environ, {"GIST_ID": "gist-identifier-12345"}):
                findings = scan_paths([path])
        self.assertTrue(any("configured secret value" in item for item in findings))

    def test_privacy_scanner_detects_secret_like_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text('{"token":"sk-abcdefghijklmnop"}', encoding="utf-8")
            self.assertTrue(scan_paths([path]))

    def test_privacy_scanner_does_not_treat_json_newline_as_drive_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text('{"summary":"point:\\nnext line"}', encoding="utf-8")
            self.assertEqual(scan_paths([path]), [])

    def test_privacy_scanner_detects_real_local_path_in_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(
                '{"path":"C:\\\\Users\\\\Private\\\\file.txt"}',
                encoding="utf-8",
            )
            self.assertTrue(scan_paths([path]))

    def test_daily_workflow_injects_optional_gist_secrets(self):
        workflow = Path(".github/workflows/daily_arxiv.yml").read_text(encoding="utf-8")
        self.assertIn("GIST_ID: ${{ secrets.GIST_ID }}", workflow)
        self.assertIn("GIST_TOKEN: ${{ secrets.GIST_TOKEN }}", workflow)

    def test_daily_workflow_uses_generic_model_secrets_and_variables(self):
        workflow = Path(".github/workflows/daily_arxiv.yml").read_text(encoding="utf-8")
        expected_references = (
            "COARSE_LLM_API_KEY: ${{ secrets.COARSE_LLM_API_KEY }}",
            "EDITORIAL_LLM_API_KEY: ${{ secrets.EDITORIAL_LLM_API_KEY }}",
            "COARSE_LLM_BASE_URL: ${{ vars.COARSE_LLM_BASE_URL }}",
            "COARSE_LLM_MODEL: ${{ vars.COARSE_LLM_MODEL }}",
            "EDITORIAL_LLM_BASE_URL: ${{ vars.EDITORIAL_LLM_BASE_URL }}",
            "EDITORIAL_LLM_MODEL: ${{ vars.EDITORIAL_LLM_MODEL }}",
        )
        for reference in expected_references:
            self.assertIn(reference, workflow)

    def test_privacy_scanner_detects_generic_api_key_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            path.write_text("COARSE_LLM_API_KEY=secret", encoding="utf-8")
            findings = scan_paths([path])
        self.assertTrue(any("API key assignment" in item for item in findings))


if __name__ == "__main__":
    unittest.main()
