"""Pure prompt assembly for memory-aware, volume-adaptive daily editions."""

from __future__ import annotations

import json
from typing import Any


def memory_bypass_rules(memory_context: list[dict[str, Any]]) -> list[str]:
    rules = [
        "逐项检查 concept_memory 后再写作，不得忽略记忆状态。",
        "status=mastered：强制跳过概念定义、历史沿革、入门类比和基础教学，直接进入今日切入视角要求的高阶分析。",
        "status=learning：只补足理解当前论文所需的最小知识缺口，重点解释新旧认知之间的差异。",
        "status=first_contact（首次接触）：允许提供精炼背景，但不得扩写成教程；背景必须服务于后续论文比较。",
    ]
    if not memory_context:
        rules.append("当前为空记忆模式：将相关概念视为首次接触，但仍保持高信息密度。")
    return rules


def _paper_for_prompt(paper: dict[str, Any], abstract_limit: int) -> dict[str, Any]:
    return {
        "paper_id": paper.get("paper_id"),
        "title": paper.get("title"),
        "abstract": str(paper.get("summary") or "")[:abstract_limit],
        "primary_topic": paper.get("primary_topic"),
        "selection_score": paper.get("selection_score"),
        "novelty_score": paper.get("novelty_score"),
        "potential_impact_score": paper.get("potential_impact_score"),
        "citation_count": paper.get("citation_count", 0),
        "venue": paper.get("venue", ""),
        "venue_tags": paper.get("venue_tags", []),
        "presentation_type": paper.get("presentation_type", ""),
        "well_known": bool(paper.get("well_known")),
        "well_known_reasons": paper.get("well_known_reasons", []),
        "historical_anchor": bool(paper.get("historical_anchor")),
        "coarse_rationale": paper.get("coarse_rationale", ""),
        "contribution_tags": paper.get("contribution_tags", []),
    }


def build_daily_edition_prompt(
    schedule: dict[str, Any],
    memory_context: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> str:
    """Build a single JSON task that lets the LLM choose edition volume."""

    policy = config["editorial_policy"]
    payload = {
        "task": "为个人智能学术智库生成一份严谨、克制、高信息密度的中文学术日报。只能依据候选论文信息，不得编造事实或实验数字。",
        "today": {
            "topic_id": schedule["topic_id"],
            "topic_name": schedule["topic_name"],
            "topic_name_en": schedule["topic_name_en"],
            "search_query": schedule["search_query"],
            "angle_id": schedule["angle_id"],
            "angle_name": schedule["angle_name"],
            "angle_instruction": schedule["angle_instruction"],
        },
        "concept_memory": memory_context,
        "memory_bypass": memory_bypass_rules(memory_context),
        "selection_policy": {
            "instruction": "根据论文的具体质量、重磅程度、互补性和今日视角，自主决定最终精选论文；不要为了凑数纳入平庸论文。",
            "min_selected_papers": int(policy["min_selected_papers"]),
            "max_selected_papers": int(policy["max_selected_papers"]),
            "max_major_features": int(config["project"]["max_major_features"]),
            "topic_balance": "多数论文应服务今日主题，但可保留真正重要且形成对照的跨主题论文。",
            "well_known_papers": {
                "definition": "仅 well_known=true 可计入；其依据必须是配置内顶会入选或引用量达到阈值。",
                "focus_domain_only": "粗筛完成后只统计 primary_topic 等于今日 topic_id 的论文；未分类候选仅用 candidate_topics 做前置保留。",
                "minimum": int(config["selection"]["min_well_known_papers"]),
                "maximum": int(config["selection"]["max_well_known_papers"]),
                "historical_requirement": "至少选择一篇 historical_anchor=true 的知名论文，确保日报不全是近期新发论文。",
            },
        },
        "volume_policy": {
            "target_total_chinese_characters": int(policy["target_total_characters"]),
            "hard_max_total_chinese_characters": int(
                policy["hard_max_total_characters"]
            ),
            "instruction": "重大论文写深、普通论文写短；避免日报过长、重复，也避免信息过于单薄。",
            "hero": "背景/痛点、核心创新、实验结论各自提供互不重复的信息。",
            "major": "保留足够上下文，但短于头条。",
            "brief": "2-3 条紧凑要点，不写长背景。",
        },
        "candidate_papers": [
            _paper_for_prompt(paper, int(policy.get("abstract_character_limit", 1800)))
            for paper in candidates
        ],
        "output_contract": {
            "format": "返回单一 JSON object，不要 Markdown fence，不要在 JSON 外输出正文。",
            "selected_papers": [
                {
                    "paper_id": "必须来自 candidate_papers，顺序即版面顺序",
                    "content_tier": "major 或 brief；至少一个 major",
                    "is_hero": "仅一个 boolean true",
                    "newspaper_title": "克制、有判断力的中文标题",
                    "dek": "高信息密度导语",
                    "background_and_pain": "major 使用；brief 为空字符串",
                    "core_innovations": ["major 使用；各条不重复"],
                    "experimental_findings": "major 使用；不得虚构数字",
                    "brief_points": ["brief 使用 2-3 条；major 为空数组"],
                    "contribution_tags": ["只能使用输入中的既有受控标签"],
                }
            ],
            "memory_payload": {
                "schema_version": 1,
                "concept_updates": [
                    {
                        "concept_id": "稳定 snake_case id",
                        "name": "概念名",
                        "status": "learning 或 mastered",
                        "mastery_level": "0.0-1.0",
                        "mastery_summary": "今日新增或升级的掌握摘要；没有更新则不列出",
                    }
                ],
            },
            "ordering_rule": "memory_payload 必须是 JSON object 的最后一个顶层字段。",
        },
    }
    return json.dumps(payload, ensure_ascii=False)
