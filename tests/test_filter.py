from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.config import ConfigError, load_config
from src.filter import (
    OpenAICompatibleClient,
    editorialize_paper,
    generate_memory_aware_edition,
    is_breakthrough,
    parse_json_response,
    prefilter_coarse_candidates,
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

    def test_openai_client_builds_provider_neutral_reasoning_payloads(self):
        cases = (
            (
                "flat",
                "max_tokens",
                {"reasoning_effort": "high"},
            ),
            (
                "nested",
                "max_completion_tokens",
                {"reasoning": {"effort": "high"}},
            ),
            ("none", "max_tokens", {}),
        )
        for reasoning_format, token_field, expected_reasoning in cases:
            with self.subTest(reasoning_format=reasoning_format):
                prefix = f"GAZETTE_TEST_{reasoning_format.upper()}"
                environment = {
                    f"{prefix}_KEY": "test-secret-value",
                    f"{prefix}_URL": "https://llm.example/v1/",
                    f"{prefix}_MODEL": "test-model",
                    f"{prefix}_TOKEN_FIELD": token_field,
                    f"{prefix}_REASONING_FORMAT": reasoning_format,
                    f"{prefix}_REASONING_EFFORT": "high",
                }
                section = {
                    "api_key_env": f"{prefix}_KEY",
                    "base_url_env": f"{prefix}_URL",
                    "model_env": f"{prefix}_MODEL",
                    "token_field_env": f"{prefix}_TOKEN_FIELD",
                    "reasoning_format_env": f"{prefix}_REASONING_FORMAT",
                    "reasoning_effort_env": f"{prefix}_REASONING_EFFORT",
                    "max_retries": 1,
                }
                session = MagicMock()
                response = session.post.return_value
                response.status_code = 200
                response.json.return_value = {
                    "choices": [{"message": {"content": "ok"}}]
                }
                with patch.dict(os.environ, environment):
                    client = OpenAICompatibleClient(section, session)
                    self.assertEqual(client.complete([], max_tokens=321), "ok")
                _, kwargs = session.post.call_args
                self.assertEqual(
                    kwargs["json"][token_field],
                    321,
                )
                self.assertEqual(kwargs["json"]["model"], "test-model")
                self.assertEqual(
                    kwargs["json"].get("reasoning_effort"),
                    expected_reasoning.get("reasoning_effort"),
                )
                self.assertEqual(
                    kwargs["json"].get("reasoning"),
                    expected_reasoning.get("reasoning"),
                )
                self.assertEqual(
                    session.post.call_args.args[0],
                    "https://llm.example/v1/chat/completions",
                )

    def test_openai_client_rejects_unknown_reasoning_format(self):
        with (
            patch.dict(
                os.environ,
                {
                    "GAZETTE_TEST_KEY": "test-secret-value",
                    "GAZETTE_TEST_URL": "https://llm.example/v1",
                    "GAZETTE_TEST_MODEL": "test-model",
                    "GAZETTE_TEST_REASONING_FORMAT": "provider-specific",
                },
            ),
            self.assertRaisesRegex(ConfigError, "reasoning_format"),
        ):
            OpenAICompatibleClient(
                {
                    "api_key_env": "GAZETTE_TEST_KEY",
                    "base_url_env": "GAZETTE_TEST_URL",
                    "model_env": "GAZETTE_TEST_MODEL",
                    "reasoning_format_env": "GAZETTE_TEST_REASONING_FORMAT",
                }
            )

    def test_daily_editor_can_choose_variable_volume_and_return_payload(self):
        papers = [self.paper(i, "cot_agentic_ai", 9 - i / 10) for i in range(5)]

        class Client:
            available = True

            def complete(self, messages, *, max_tokens=1800):
                del messages, max_tokens
                selected = []
                for index, paper in enumerate(papers[:3]):
                    major = index == 0
                    selected.append(
                        {
                            "paper_id": paper["paper_id"],
                            "content_tier": "major" if major else "brief",
                            "is_hero": major,
                            "newspaper_title": f"精选 {index}",
                            "dek": "导语",
                            "background_and_pain": "背景" if major else "",
                            "core_innovations": ["创新"] if major else [],
                            "experimental_findings": "结论" if major else "",
                            "brief_points": [] if major else ["要点一", "要点二"],
                            "contribution_tags": [],
                        }
                    )
                return __import__("json").dumps(
                    {
                        "selected_papers": selected,
                        "memory_payload": {
                            "schema_version": 1,
                            "concept_updates": [],
                        },
                    },
                    ensure_ascii=False,
                )

        schedule = {
            "topic_id": "cot_agentic_ai",
            "topic_name": "CoT 与 Agent 实现",
            "topic_name_en": "CoT & Agentic AI",
            "search_query": "query",
            "angle_id": "first_principles",
            "angle_name": "底层理论",
            "angle_instruction": "分析假设",
        }
        selected, meta, payload = generate_memory_aware_edition(
            papers, self.target_date, self.config, schedule, [], Client()
        )
        self.assertEqual(len(selected), 3)
        self.assertEqual(meta["edition_selection_status"], "available_daily")
        self.assertEqual(payload["schema_version"], 1)

    def test_invalid_daily_editor_output_uses_stable_fallback(self):
        papers = [self.paper(i, "cot_agentic_ai", 9 - i / 10) for i in range(7)]

        class Client:
            available = True

            def complete(self, messages, *, max_tokens=1800):
                del messages, max_tokens
                return '{"memory_payload":{},"selected_papers":[]}'

        schedule = {
            "topic_id": "cot_agentic_ai",
            "topic_name": "CoT 与 Agent 实现",
            "topic_name_en": "CoT & Agentic AI",
            "search_query": "query",
            "angle_id": "first_principles",
            "angle_name": "底层理论",
            "angle_instruction": "分析假设",
        }
        selected, meta, payload = generate_memory_aware_edition(
            papers, self.target_date, self.config, schedule, [], Client()
        )
        self.assertEqual(len(selected), 7)
        self.assertEqual(
            meta["edition_selection_status"], "fallback_invalid_daily_json"
        )
        self.assertEqual(payload["concept_updates"], [])

    def test_prefilter_bounds_llm_candidates_and_preserves_focus(self):
        papers = [
            {
                **self.paper(index, "cot_agentic_ai", 8),
                "candidate_topics": ["cot_agentic_ai"],
                "title": f"Agent planning {index}",
            }
            for index in range(25)
        ]
        papers += [
            {
                **self.paper(100 + index, "world_models", 8),
                "candidate_topics": ["world_models"],
                "title": f"World model {index}",
            }
            for index in range(25)
        ]
        self.config["selection"]["coarse_candidate_limit"] = 12
        self.config["selection"]["coarse_focus_minimum"] = 8
        selected = prefilter_coarse_candidates(
            papers, self.target_date, self.config, "cot_agentic_ai"
        )
        self.assertEqual(len(selected), 12)
        self.assertGreaterEqual(
            sum("cot_agentic_ai" in paper["candidate_topics"] for paper in selected),
            8,
        )


if __name__ == "__main__":
    unittest.main()
