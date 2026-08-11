from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.config import load_config
from src.scheduler import (
    load_schedule_history,
    schedule_daily_focus,
    topic_query_order,
)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.target_date = date(2026, 8, 11)

    def test_same_date_and_history_are_deterministic(self):
        history = [
            {
                "date": "2026-08-10",
                "topic_id": "cot_agentic_ai",
                "angle_id": "first_principles",
            }
        ]
        first = schedule_daily_focus(self.target_date, self.config, history=history)
        second = schedule_daily_focus(self.target_date, self.config, history=history)
        self.assertEqual(first, second)

    def test_starvation_guard_chooses_longest_unselected_topic(self):
        history = []
        for topic_id in self.config["scheduler"]["topic_pool"]:
            last_seen = "2026-08-01"
            if topic_id == "causal_inference":
                last_seen = "2026-06-01"
            history.append({"date": last_seen, "topic_id": topic_id})
        decision = schedule_daily_focus(self.target_date, self.config, history=history)
        self.assertEqual(decision.topic_id, "causal_inference")
        self.assertEqual(decision.selection_reason, "starvation_guard")

    def test_scheduling_does_not_accept_paper_volume_input(self):
        decision = schedule_daily_focus(self.target_date, self.config, history=[])
        self.assertIn(decision.topic_id, self.config["scheduler"]["topic_pool"])
        self.assertTrue(decision.search_query)
        self.assertEqual(topic_query_order(decision, self.config)[0], decision.topic_id)

    def test_history_ignores_current_date_for_force_reruns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / self.config["render"]["json_dir"]
            report_dir.mkdir(parents=True)
            (report_dir / "old.json").write_text(
                '{"schema_version":2,"report":{"date":"2026-08-10","focus_topic":"world_models","angle_id":"systems_tradeoffs"}}',
                encoding="utf-8",
            )
            (report_dir / "today.json").write_text(
                '{"schema_version":2,"report":{"date":"2026-08-11","focus_topic":"causal_inference","angle_id":"first_principles"}}',
                encoding="utf-8",
            )
            history = load_schedule_history(
                self.config, self.target_date, project_root=root
            )
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["topic_id"], "world_models")


if __name__ == "__main__":
    unittest.main()
