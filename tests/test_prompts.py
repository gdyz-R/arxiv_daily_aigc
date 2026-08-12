from __future__ import annotations

import json
import unittest

from src.config import load_config
from src.prompts import build_daily_edition_prompt
from src.scheduler import schedule_daily_focus


class PromptTests(unittest.TestCase):
    def test_prompt_contains_memory_bypass_volume_and_payload_contract(self):
        config = load_config()
        schedule = schedule_daily_focus(
            __import__("datetime").date(2026, 8, 11), config, history=[]
        ).as_dict()
        prompt = json.loads(
            build_daily_edition_prompt(
                schedule,
                [
                    {
                        "concept_id": "kv_cache",
                        "status": "mastered",
                        "mastery_level": 0.9,
                        "mastery_summary": "已掌握分页缓存。",
                    },
                    {
                        "concept_id": "cache_eviction",
                        "status": "first_contact",
                        "mastery_level": 0,
                        "mastery_summary": "",
                    },
                ],
                [{"paper_id": "p1", "title": "Paper", "summary": "Abstract"}],
                config,
            )
        )
        bypass = " ".join(prompt["memory_bypass"])
        self.assertIn("强制跳过", bypass)
        self.assertIn("首次接触", bypass)
        self.assertEqual(prompt["selection_policy"]["min_selected_papers"], 3)
        self.assertEqual(prompt["selection_policy"]["max_selected_papers"], 7)
        self.assertEqual(prompt["selection_policy"]["well_known_papers"]["minimum"], 1)
        self.assertEqual(prompt["selection_policy"]["well_known_papers"]["maximum"], 3)
        self.assertIn(
            "historical_anchor",
            prompt["selection_policy"]["well_known_papers"]["historical_requirement"],
        )
        self.assertIn("memory_payload", prompt["output_contract"])
        self.assertIn(
            schedule["angle_instruction"], prompt["today"]["angle_instruction"]
        )


if __name__ == "__main__":
    unittest.main()
