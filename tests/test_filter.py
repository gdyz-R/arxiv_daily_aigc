from __future__ import annotations

import unittest
from datetime import date

from src.config import load_config
from src.filter import (
    OpenAICompatibleClient,
    editorialize_paper,
    is_breakthrough,
    parse_json_response,
    select_daily_papers,
)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.target_date = date(2026, 8, 12)  # Wednesday -> CoT & Agentic AI

    def paper(self, index: int, topic: str, score: float) -> dict:
        return {
            "paper_id": f"paper:{index}",
            "title": f"Paper {index}",
            "summary": "We propose a method and report experiments.",
            "published_date": "2026-08-10T00:00:00+00:00",
            "primary_topic": topic,
            "topic_scores": {topic: score},
            "novelty_score": score,
            "potential_impact_score": score,
            "clarity_score": 8,
            "is_relevant": True,
            "citation_count": 0,
            "venue_tags": [],
        }

    def test_six_plus_one_selection(self):
        papers = [self.paper(i, "cot_agentic_ai", 9 - i / 10) for i in range(8)]
        papers += [
            self.paper(20 + i, "subquadratic_attention", 8 - i / 10) for i in range(3)
        ]
        selected, meta = select_daily_papers(papers, self.target_date, self.config)
        self.assertEqual(len(selected), 7)
        self.assertEqual(meta["actual_focus_count"], 6)
        self.assertEqual(meta["actual_cross_topic_count"], 1)
        self.assertEqual(sum(p["content_tier"] == "major" for p in selected), 2)

    def test_breakthrough_threshold_is_strict(self):
        self.assertFalse(is_breakthrough({"novelty_score": 8.5}, self.config))
        self.assertTrue(is_breakthrough({"novelty_score": 8.51}, self.config))

    def test_top_venue_marks_major(self):
        papers = [self.paper(i, "cot_agentic_ai", 8) for i in range(7)]
        papers[3]["venue_tags"] = ["NeurIPS"]
        selected, _ = select_daily_papers(papers, self.target_date, self.config)
        top_venue = next(p for p in selected if p["paper_id"] == "paper:3")
        self.assertEqual(top_venue["major_reason"], "top_venue")

    def test_json_fence_parser(self):
        self.assertEqual(
            parse_json_response('prefix```json\n{"score": 9}\n```'), {"score": 9}
        )

    def test_major_fallback_has_distinct_sections(self):
        paper = {
            "title": "Adaptive Agent Planning",
            "summary": (
                "Long-horizon agents often waste compute on simple decisions. "
                "We propose an adaptive planning framework that allocates reasoning depth by uncertainty. "
                "The method introduces a recoverability-aware action score. "
                "Experiments demonstrate higher task success at the same compute budget. "
                "The evaluation shows fewer repeated tool calls."
            ),
            "primary_topic": "cot_agentic_ai",
            "content_tier": "major",
            "is_hero": False,
            "contribution_tags": ["Planning"],
        }
        client = OpenAICompatibleClient(
            {"api_key_env": "TEST_MISSING_EDITORIAL_KEY", "model": "unused"}
        )
        editorialize_paper(paper, self.config, client)
        self.assertTrue(paper["background_and_pain"])
        self.assertTrue(paper["core_innovations"])
        self.assertTrue(paper["experimental_findings"])
        self.assertNotEqual(
            paper["background_and_pain"], paper["experimental_findings"]
        )


if __name__ == "__main__":
    unittest.main()
