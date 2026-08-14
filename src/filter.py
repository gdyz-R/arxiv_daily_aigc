"""Paper classification, selection, headline judgement and editorial generation."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any, Protocol

import requests

try:
    from config import ConfigError, environment_value_from, load_config, secret_from
    from prominence import (
        is_focus_well_known,
        prominence_policy_errors,
        prominence_summary,
    )
    from prompts import build_daily_edition_prompt
except ImportError:  # pragma: no cover
    from .config import ConfigError, environment_value_from, load_config, secret_from
    from .prominence import (
        is_focus_well_known,
        prominence_policy_errors,
        prominence_summary,
    )
    from .prompts import build_daily_edition_prompt


LOGGER = logging.getLogger(__name__)
WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class CompletionClient(Protocol):
    """Small interface shared by the runtime client and deterministic test doubles."""

    @property
    def available(self) -> bool: ...

    def complete(
        self, messages: list[dict[str, Any]], *, max_tokens: int = 1800
    ) -> str | None: ...


def focus_topic_for_date(target_date: date, config: dict[str, Any]) -> str:
    return config["topic_rotation"][WEEKDAY_NAMES[target_date.weekday()]]


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        return round(min(max(float(value), 0.0), 10.0), 2)
    except (TypeError, ValueError):
        return default


def parse_json_response(text: str) -> Any:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if starts:
        text = text[min(starts) :]
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[: end + 1] if end >= 0 else text)


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible client with configuration-driven request fields."""

    def __init__(
        self, section: dict[str, Any], session: requests.Session | None = None
    ):
        self.section = section
        self.api_key = secret_from(section)
        self.base_url = str(
            environment_value_from(section, "base_url", "") or ""
        ).rstrip("/")
        self.model = str(environment_value_from(section, "model", "") or "").strip()
        self.token_field = str(
            environment_value_from(section, "token_field", "max_tokens") or "max_tokens"
        ).strip()
        self.reasoning_format = (
            str(environment_value_from(section, "reasoning_format", "none") or "none")
            .strip()
            .lower()
        )
        self.reasoning_effort = str(
            environment_value_from(section, "reasoning_effort", "") or ""
        ).strip()
        if self.reasoning_format not in {"none", "flat", "nested"}:
            raise ConfigError("reasoning_format must be one of: none, flat, nested")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.token_field):
            raise ConfigError("token_field must be a valid JSON field name")
        if self.api_key and (not self.base_url or not self.model):
            raise ConfigError(
                "An enabled LLM requires both base_url and model environment variables"
            )
        self.session = session or requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def complete(
        self, messages: list[dict[str, Any]], *, max_tokens: int = 1800
    ) -> str | None:
        if not self.available:
            return None
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.section.get("temperature", 0.2),
            "response_format": {"type": "json_object"},
        }
        payload[self.token_field] = max_tokens
        if self.reasoning_effort and self.reasoning_format == "flat":
            payload["reasoning_effort"] = self.reasoning_effort
        elif self.reasoning_effort and self.reasoning_format == "nested":
            payload["reasoning"] = {"effort": self.reasoning_effort}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        retries = int(self.section.get("max_retries", 3))
        for attempt in range(retries):
            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=float(self.section.get("timeout_seconds", 120)),
                )
                if response.status_code == 429:
                    time.sleep(min(2 ** (attempt + 1), 30))
                    continue
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"].strip()
            except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
                status_code = getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                error_detail = type(exc).__name__
                if status_code is not None:
                    error_detail += f"(status={status_code})"
                LOGGER.warning(
                    "%s API attempt %s/%s failed: %s",
                    self.section.get("role", "llm"),
                    attempt + 1,
                    retries,
                    error_detail,
                )
                if attempt + 1 < retries:
                    time.sleep(min(2**attempt, 10))
        return None


def keyword_topic_scores(
    paper: dict[str, Any], config: dict[str, Any]
) -> dict[str, float]:
    title = str(paper.get("title", "")).lower()
    abstract = str(paper.get("summary", "")).lower()
    categories = set(paper.get("categories", []))
    candidates = set(paper.get("candidate_topics", []))
    scores: dict[str, float] = {}
    for topic_id, topic in config["topics"].items():
        title_hits = sum(keyword.lower() in title for keyword in topic["keywords"])
        abstract_hits = sum(
            keyword.lower() in abstract for keyword in topic["keywords"]
        )
        score = (
            1.8 * title_hits
            + 0.75 * abstract_hits
            + 0.6 * bool(categories & set(topic["categories"]))
        )
        if topic_id in candidates:
            score += 1.5
        scores[topic_id] = round(min(score, 10.0), 2)
    return scores


def _allowed_tags(
    requested: list[Any], paper: dict[str, Any], config: dict[str, Any]
) -> list[str]:
    allowlist = config["selection"].get("contribution_tag_allowlist", [])
    result = [str(tag) for tag in requested if str(tag) in allowlist]
    if result:
        return result[:4]
    text = f"{paper.get('title', '')} {paper.get('summary', '')}".lower()
    return [tag for tag in allowlist if tag.lower() in text][:4]


def _fallback_coarse(paper: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    scores = keyword_topic_scores(paper, config)
    primary = max(scores, key=lambda topic_id: scores[topic_id])
    abstract = str(paper.get("summary", "")).lower()
    markers = (
        "we introduce",
        "we propose",
        "first",
        "novel",
        "state-of-the-art",
        "outperform",
        "theoretically show",
    )
    novelty = 5.5 + min(sum(marker in abstract for marker in markers) * 0.65, 3.5)
    return {
        "is_relevant": scores[primary] >= 1.5,
        "primary_topic": primary,
        "topic_scores": scores,
        "novelty_score": round(novelty, 2),
        "potential_impact_score": round(min(5 + scores[primary] / 2, 9.0), 2),
        "clarity_score": 7.0 if len(abstract) > 250 else 5.5,
        "coarse_rationale": "规则降级：依据标题、摘要关键词和论文分类评分。",
        "contribution_tags": _allowed_tags([], paper, config),
        "coarse_model_status": "fallback_rules",
    }


def coarse_classify_paper(
    paper: dict[str, Any], config: dict[str, Any], client: CompletionClient
) -> dict[str, Any]:
    fallback = _fallback_coarse(paper, config)
    if not client.available:
        paper.update(fallback)
        return paper
    topic_reference = {
        topic_id: {"name": topic["name_en"], "keywords": topic["keywords"]}
        for topic_id, topic in config["topics"].items()
    }
    prompt = {
        "task": "Classify and score one AI research paper for a personal daily newspaper.",
        "topics": topic_reference,
        "paper": {
            "title": paper.get("title"),
            "abstract": paper.get("summary"),
            "categories": paper.get("categories", []),
            "venue": paper.get("venue", ""),
        },
        "output": {
            "is_relevant": "boolean",
            "primary_topic": "exact topic key",
            "topic_scores": "all topic keys scored 0-10",
            "novelty_score": "1-10",
            "potential_impact_score": "1-10",
            "clarity_score": "1-10",
            "coarse_rationale": "one Chinese sentence",
            "contribution_tags": "0-4 controlled tags",
        },
    }
    response = client.complete(
        [
            {
                "role": "system",
                "content": "You are a conservative research-paper triage editor. Return JSON only.",
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        max_tokens=1000,
    )
    if not response:
        paper.update(fallback)
        return paper
    try:
        result = parse_json_response(response)
        topic_scores = {
            topic_id: _clamp_score((result.get("topic_scores") or {}).get(topic_id))
            for topic_id in config["topics"]
        }
        primary = result.get("primary_topic")
        if primary not in config["topics"]:
            primary = max(topic_scores, key=lambda topic_id: topic_scores[topic_id])
        primary = str(primary)
        paper.update(
            {
                "is_relevant": bool(
                    result.get("is_relevant", max(topic_scores.values()) >= 5)
                ),
                "primary_topic": primary,
                "topic_scores": topic_scores,
                "novelty_score": _clamp_score(
                    result.get("novelty_score"), fallback["novelty_score"]
                ),
                "potential_impact_score": _clamp_score(
                    result.get("potential_impact_score"),
                    fallback["potential_impact_score"],
                ),
                "clarity_score": _clamp_score(
                    result.get("clarity_score"), fallback["clarity_score"]
                ),
                "coarse_rationale": str(result.get("coarse_rationale", ""))[:300],
                "contribution_tags": _allowed_tags(
                    result.get("contribution_tags") or [], paper, config
                ),
                "coarse_model_status": "available",
            }
        )
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        LOGGER.warning("Invalid coarse JSON; using fallback: %s", type(exc).__name__)
        paper.update(fallback)
    return paper


def coarse_classify_papers(
    papers: list[dict[str, Any]], config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    config = config or load_config()
    client = OpenAICompatibleClient(config["llm"]["coarse"])
    for index, paper in enumerate(papers, 1):
        LOGGER.info("Coarse classification %s/%s", index, len(papers))
        coarse_classify_paper(paper, config, client)
    return papers


def prefilter_coarse_candidates(
    papers: list[dict[str, Any]],
    target_date: date,
    config: dict[str, Any],
    focus_topic: str,
) -> list[dict[str, Any]]:
    """Bound expensive LLM triage while preserving focus and cross-topic coverage."""

    limit = int(config["selection"].get("coarse_candidate_limit", 36))
    focus_minimum = min(int(config["selection"].get("coarse_focus_minimum", 18)), limit)
    ranked: list[dict[str, Any]] = []
    for paper in papers:
        scores = keyword_topic_scores(paper, config)
        focus_relevance = scores.get(focus_topic, 0.0)
        best_relevance = max(scores.values(), default=0.0)
        paper["prefilter_topic_scores"] = scores
        paper["prefilter_score"] = round(
            focus_relevance * 1.3
            + best_relevance
            + _recency_score(paper, target_date) * 0.35
            + min(math.log1p(max(int(paper.get("citation_count", 0)), 0)), 5.0),
            3,
        )
        ranked.append(paper)
    ranked.sort(
        key=lambda paper: (
            paper.get("prefilter_score", 0),
            paper.get("published_date", ""),
        ),
        reverse=True,
    )
    if len(papers) <= limit:
        return ranked
    historical_focus = [
        paper
        for paper in ranked
        if paper.get("historical_anchor") and is_focus_well_known(paper, focus_topic)
    ]
    known_focus = [
        paper
        for paper in ranked
        if is_focus_well_known(paper, focus_topic) and paper not in historical_focus
    ]
    reserved = (historical_focus[:1] + known_focus)[
        : int(config["selection"].get("max_well_known_papers", 3))
    ]
    focus = [
        paper
        for paper in ranked
        if focus_topic in paper.get("candidate_topics", [])
        or (paper.get("prefilter_topic_scores") or {}).get(focus_topic, 0) > 0
    ]
    selected = list(reserved)
    selected_ids = {paper.get("paper_id") or id(paper) for paper in selected}
    for paper in focus:
        if len(selected) >= focus_minimum:
            break
        identity = paper.get("paper_id") or id(paper)
        if identity not in selected_ids:
            selected.append(paper)
            selected_ids.add(identity)
    for paper in ranked:
        if len(selected) >= limit:
            break
        identity = paper.get("paper_id") or id(paper)
        if identity not in selected_ids:
            selected.append(paper)
            selected_ids.add(identity)
    LOGGER.info(
        "Prefilter retained %s/%s candidates for LLM coarse classification",
        len(selected),
        len(papers),
    )
    return selected


def _recency_score(paper: dict[str, Any], target_date: date) -> float:
    published = _parse_datetime(paper.get("published_date"))
    if not published:
        return 0.0
    return max(0.0, 10.0 - max((target_date - published.date()).days, 0) * 0.18)


def calculate_selection_score(
    paper: dict[str, Any], target_date: date, focus_topic: str
) -> float:
    primary = str(paper.get("primary_topic") or "")
    relevance = _clamp_score((paper.get("topic_scores") or {}).get(primary))
    score = (
        relevance * 0.34
        + _clamp_score(paper.get("novelty_score")) * 0.24
        + _clamp_score(paper.get("potential_impact_score")) * 0.18
        + _recency_score(paper, target_date) * 0.14
        + math.log1p(max(int(paper.get("citation_count", 0)), 0)) * 0.55
        + (1.2 if primary == focus_topic else 0.0)
        + min(len(paper.get("venue_tags", [])) * 1.2, 2.4)
        + {"Oral": 2.0, "Spotlight": 1.5, "Poster": 0.35}.get(
            str(paper.get("presentation_type") or ""), 0.0
        )
    )
    return round(score, 3)


def is_breakthrough(paper: dict[str, Any], config: dict[str, Any]) -> bool:
    return _clamp_score(paper.get("novelty_score")) > float(
        config["selection"].get("breakthrough_threshold", 8.5)
    )


def is_major_feature(
    paper: dict[str, Any], target_date: date, config: dict[str, Any]
) -> tuple[bool, str]:
    if is_breakthrough(paper, config):
        return True, "breakthrough"
    if paper.get("venue_tags"):
        return True, "top_venue"
    if paper.get("presentation_type") in {"Oral", "Spotlight"}:
        return True, "official_presentation"
    citations = int(paper.get("citation_count", 0))
    published = _parse_datetime(paper.get("published_date"))
    age_days = (target_date - published.date()).days if published else 9999
    threshold = int(config["selection"].get("high_citation_threshold", 100))
    if age_days <= int(config["selection"].get("recent_days", 120)):
        threshold = int(config["selection"].get("recent_high_citation_threshold", 20))
    return (True, "high_citation") if citations >= threshold else (False, "brief")


def select_daily_papers(
    papers: list[dict[str, Any]],
    target_date: date,
    config: dict[str, Any] | None = None,
    *,
    focus_topic: str | None = None,
    target_count: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = config or load_config()
    focus_topic = focus_topic or focus_topic_for_date(target_date, config)
    eligible: list[dict[str, Any]] = []
    for paper in papers:
        if paper.get("eligible_by_date") is False:
            continue
        if not paper.get("primary_topic"):
            paper.update(_fallback_coarse(paper, config))
        if not paper.get("is_relevant", True):
            continue
        paper["selection_score"] = calculate_selection_score(
            paper, target_date, focus_topic
        )
        eligible.append(paper)
    ranked = sorted(
        eligible,
        key=lambda item: (
            item.get("selection_score", 0),
            item.get("novelty_score", 0),
            item.get("published_date", ""),
        ),
        reverse=True,
    )
    focus_target = int(config["project"]["focus_count"])
    cross_target = int(config["project"]["cross_topic_count"])
    total_target = int(
        config["project"]["edition_size"] if target_count is None else target_count
    )
    total_target = max(total_target, 0)
    if target_count is not None:
        focus_target = min(focus_target, total_target)
        cross_target = min(cross_target, max(total_target - focus_target, 0))
    focus_pool = [
        paper for paper in ranked if paper.get("primary_topic") == focus_topic
    ]
    cross_pool = [
        paper for paper in ranked if paper.get("primary_topic") != focus_topic
    ]
    selected = focus_pool[:focus_target] + cross_pool[:cross_target]
    selected_ids = {paper.get("paper_id") or id(paper) for paper in selected}
    for paper in ranked:
        if len(selected) >= total_target:
            break
        identity = paper.get("paper_id") or id(paper)
        if identity not in selected_ids:
            selected.append(paper)
            selected_ids.add(identity)

    def ensure_selected(required: dict[str, Any]) -> None:
        identity = required.get("paper_id") or id(required)
        if identity in selected_ids or total_target <= 0:
            return
        replaceable = [
            paper
            for paper in reversed(selected)
            if not (
                paper.get("historical_anchor")
                and is_focus_well_known(paper, focus_topic)
            )
        ]
        if len(selected) < total_target:
            selected.append(required)
        elif replaceable:
            replaced = replaceable[0]
            selected.remove(replaced)
            selected_ids.discard(replaced.get("paper_id") or id(replaced))
            selected.append(required)
        else:
            return
        selected_ids.add(identity)

    historical_focus = [
        paper
        for paper in ranked
        if paper.get("historical_anchor") and is_focus_well_known(paper, focus_topic)
    ]
    known_focus = [paper for paper in ranked if is_focus_well_known(paper, focus_topic)]
    if historical_focus:
        ensure_selected(historical_focus[0])
    minimum_known = int(config["selection"].get("min_well_known_papers", 1))
    for paper in known_focus:
        if (
            sum(is_focus_well_known(item, focus_topic) for item in selected)
            >= minimum_known
        ):
            break
        ensure_selected(paper)

    maximum_known = int(config["selection"].get("max_well_known_papers", 3))
    while (
        sum(is_focus_well_known(item, focus_topic) for item in selected) > maximum_known
    ):
        historical_count = sum(
            is_focus_well_known(paper, focus_topic)
            and bool(paper.get("historical_anchor"))
            for paper in selected
        )
        extra = next(
            (
                paper
                for paper in reversed(selected)
                if is_focus_well_known(paper, focus_topic)
                and (not paper.get("historical_anchor") or historical_count > 1)
            ),
            None,
        )
        replacement = next(
            (
                paper
                for paper in ranked
                if not is_focus_well_known(paper, focus_topic)
                and (paper.get("paper_id") or id(paper)) not in selected_ids
            ),
            None,
        )
        if not extra:
            break
        selected.remove(extra)
        selected_ids.discard(extra.get("paper_id") or id(extra))
        if replacement:
            selected.append(replacement)
            selected_ids.add(replacement.get("paper_id") or id(replacement))
    selected.sort(key=lambda item: item.get("selection_score", 0), reverse=True)
    major_candidates = []
    for paper in selected:
        major, reason = is_major_feature(paper, target_date, config)
        paper.update(
            {
                "major_candidate": major,
                "major_reason": reason,
                "breakthrough": reason == "breakthrough",
            }
        )
        if major:
            major_candidates.append(paper)
    if not major_candidates and selected:
        selected[0].update({"major_candidate": True, "major_reason": "editor_pick"})
        major_candidates = [selected[0]]
    max_major = int(config["project"].get("max_major_features", 2))
    major_ids = {
        paper.get("paper_id") or id(paper)
        for paper in sorted(
            major_candidates,
            key=lambda item: (
                item.get("breakthrough", False),
                item.get("selection_score", 0),
            ),
            reverse=True,
        )[:max_major]
    }
    hero_assigned = False
    for paper in selected:
        paper["content_tier"] = (
            "major" if (paper.get("paper_id") or id(paper)) in major_ids else "brief"
        )
        paper["is_hero"] = paper["content_tier"] == "major" and not hero_assigned
        hero_assigned = hero_assigned or paper["is_hero"]
    actual_focus = sum(paper.get("primary_topic") == focus_topic for paper in selected)
    meta = {
        "focus_topic": focus_topic,
        "target_focus_count": focus_target,
        "target_cross_topic_count": cross_target,
        "actual_focus_count": actual_focus,
        "actual_cross_topic_count": len(selected) - actual_focus,
        "candidate_count": len(eligible),
        "selected_count": len(selected),
        "distribution_note": "主主题候选不足，已按综合评分回填。"
        if actual_focus < min(focus_target, len(selected))
        else "按 6+1 主/跨主题策略选取。",
        **prominence_summary(selected, focus_topic),
    }
    return selected, meta


def rank_candidate_shortlist(
    papers: list[dict[str, Any]],
    target_date: date,
    config: dict[str, Any],
    focus_topic: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create a balanced model shortlist larger than the final edition."""

    return select_daily_papers(
        papers,
        target_date,
        config,
        focus_topic=focus_topic,
        target_count=int(config["editorial_policy"]["candidate_shortlist_size"]),
    )


def _limited_string(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _daily_editorial_fields(
    result: dict[str, Any],
    paper: dict[str, Any],
    config: dict[str, Any],
    *,
    tier: str,
    hero: bool,
) -> dict[str, Any]:
    if hero:
        limits = {
            "title": 90,
            "dek": 150,
            "background": 430,
            "innovation": 140,
            "findings": 430,
        }
    elif tier == "major":
        limits = {
            "title": 80,
            "dek": 130,
            "background": 330,
            "innovation": 120,
            "findings": 330,
        }
    else:
        limits = {
            "title": 80,
            "dek": 110,
            "background": 0,
            "innovation": 0,
            "findings": 0,
        }
    innovations = result.get("core_innovations") or []
    brief_points = result.get("brief_points") or []
    if not isinstance(innovations, list) or not isinstance(brief_points, list):
        raise TypeError("Daily editorial list fields must be arrays")
    return {
        "newspaper_title": _limited_string(
            result.get("newspaper_title") or paper.get("title"), limits["title"]
        ),
        "dek": _limited_string(
            result.get("dek") or paper.get("coarse_rationale") or paper.get("summary"),
            limits["dek"],
        ),
        "background_and_pain": _limited_string(
            result.get("background_and_pain"), limits["background"]
        )
        if tier == "major"
        else "",
        "core_innovations": [
            _limited_string(item, limits["innovation"])
            for item in innovations[: (4 if hero else 3)]
            if _limited_string(item, limits["innovation"])
        ]
        if tier == "major"
        else [],
        "experimental_findings": _limited_string(
            result.get("experimental_findings"), limits["findings"]
        )
        if tier == "major"
        else "",
        "brief_points": [
            _limited_string(item, 100)
            for item in brief_points[:3]
            if _limited_string(item, 100)
        ]
        if tier == "brief"
        else [],
        "contribution_tags": _allowed_tags(
            result.get("contribution_tags") or [], paper, config
        ),
        "editorial_model_status": "available_daily",
    }


def _editorial_character_count(papers: list[dict[str, Any]]) -> int:
    fields = (
        "newspaper_title",
        "dek",
        "background_and_pain",
        "experimental_findings",
    )
    return sum(
        sum(len(str(paper.get(field) or "")) for field in fields)
        + sum(len(str(item)) for item in paper.get("core_innovations", []))
        + sum(len(str(item)) for item in paper.get("brief_points", []))
        for paper in papers
    )


def _parse_daily_edition_result(
    response: str,
    candidates: list[dict[str, Any]],
    target_date: date,
    config: dict[str, Any],
    focus_topic: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    result = parse_json_response(response)
    if not isinstance(result, dict):
        raise TypeError("Daily edition response must be an object")
    if not result or next(reversed(result)) != "memory_payload":
        raise ValueError("memory_payload must be the final top-level field")
    requested = result.get("selected_papers")
    if not isinstance(requested, list):
        raise TypeError("selected_papers must be an array")
    policy = config["editorial_policy"]
    minimum = int(policy["min_selected_papers"])
    maximum = int(policy["max_selected_papers"])
    if not minimum <= len(requested) <= maximum:
        raise ValueError("Model selected an invalid number of papers")
    candidate_map = {str(paper.get("paper_id")): paper for paper in candidates}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    hero_count = 0
    major_count = 0
    for raw in requested:
        if not isinstance(raw, dict):
            raise TypeError("Selected paper entries must be objects")
        paper_id = str(raw.get("paper_id") or "")
        if paper_id not in candidate_map or paper_id in seen:
            raise ValueError("Model selected an unknown or duplicate paper")
        seen.add(paper_id)
        tier = str(raw.get("content_tier") or "brief")
        if tier not in {"major", "brief"}:
            raise ValueError("Invalid content tier")
        hero = bool(raw.get("is_hero"))
        if hero and tier != "major":
            raise ValueError("The hero paper must be a major feature")
        hero_count += int(hero)
        major_count += int(tier == "major")
        paper = deepcopy(candidate_map[paper_id])
        paper.update(
            {
                "content_tier": tier,
                "is_hero": hero,
                "major_candidate": tier == "major",
                "major_reason": "daily_editor_pick" if tier == "major" else "brief",
                "breakthrough": is_breakthrough(paper, config),
                **_daily_editorial_fields(raw, paper, config, tier=tier, hero=hero),
            }
        )
        if tier == "major" and not paper.get("core_innovations"):
            raise ValueError("Major paper lacks core innovations")
        if tier == "brief" and not paper.get("brief_points"):
            raise ValueError("Brief paper lacks brief points")
        selected.append(paper)
    if hero_count != 1 or major_count < 1:
        raise ValueError(
            "Daily edition requires exactly one hero and at least one major"
        )
    if major_count > int(config["project"]["max_major_features"]):
        raise ValueError("Daily edition contains too many major features")
    prominence_errors = prominence_policy_errors(selected, focus_topic, config)
    if prominence_errors:
        raise ValueError("; ".join(prominence_errors))
    character_count = _editorial_character_count(selected)
    if character_count > int(policy["hard_max_total_characters"]):
        raise ValueError("Daily edition exceeds the configured hard volume limit")
    actual_focus = sum(paper.get("primary_topic") == focus_topic for paper in selected)
    meta = {
        "focus_topic": focus_topic,
        "target_focus_count": None,
        "target_cross_topic_count": None,
        "actual_focus_count": actual_focus,
        "actual_cross_topic_count": len(selected) - actual_focus,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "distribution_note": f"模型按质量自主精选 {len(selected)} 篇，并执行动态篇幅控制。",
        "edition_selection_status": "available_daily",
        "editorial_character_count": character_count,
        **prominence_summary(selected, focus_topic),
    }
    memory_payload = result.get("memory_payload")
    if not isinstance(memory_payload, dict):
        raise TypeError("memory_payload must be an object")
    return selected, meta, memory_payload


def _fallback_daily_edition(
    candidates: list[dict[str, Any]],
    target_date: date,
    config: dict[str, Any],
    focus_topic: str,
    client: CompletionClient,
    reason: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    selected, meta = select_daily_papers(
        candidates, target_date, config, focus_topic=focus_topic
    )
    for paper in selected:
        editorialize_paper(paper, config, client)
    meta["edition_selection_status"] = reason
    meta["distribution_note"] = (
        f"日级自主选文不可用（{reason}），已回退到稳定的规则选文与逐篇精编。"
    )
    meta["editorial_character_count"] = _editorial_character_count(selected)
    return selected, meta, {"schema_version": 1, "concept_updates": []}


def generate_memory_aware_edition(
    candidates: list[dict[str, Any]],
    target_date: date,
    config: dict[str, Any],
    schedule: dict[str, Any],
    memory_context: list[dict[str, Any]],
    client: CompletionClient | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Generate one variable-volume edition and return its private memory payload."""

    client = client or OpenAICompatibleClient(config["llm"]["editorial"])
    focus_topic = str(schedule["topic_id"])
    if not client.available:
        return _fallback_daily_edition(
            candidates,
            target_date,
            config,
            focus_topic,
            client,
            "fallback_no_editorial_key",
        )
    prompt = build_daily_edition_prompt(schedule, memory_context, candidates, config)
    response = client.complete(
        [
            {
                "role": "system",
                "content": "You are the senior editor of a memory-aware AI research intelligence brief. Obey the memory bypass rules and return JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=int(config["editorial_policy"].get("max_completion_tokens", 7000)),
    )
    if not response:
        return _fallback_daily_edition(
            candidates,
            target_date,
            config,
            focus_topic,
            client,
            "fallback_daily_api_failure",
        )
    try:
        return _parse_daily_edition_result(
            response, candidates, target_date, config, focus_topic
        )
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
        LOGGER.warning(
            "Invalid daily edition JSON; using stable fallback: %s", type(exc).__name__
        )
        return _fallback_daily_edition(
            candidates,
            target_date,
            config,
            focus_topic,
            client,
            "fallback_invalid_daily_json",
        )


def _abstract_sentences(summary: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", summary).strip()
    if not normalized:
        return []
    sentences = re.split(r"(?<=[.!?。！？])\s+", normalized)
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def _join_limited(sentences: list[str], limit: int) -> str:
    result = ""
    for sentence in sentences:
        candidate = f"{result} {sentence}".strip()
        if result and len(candidate) > limit:
            break
        result = candidate
    if not result and sentences:
        result = sentences[0][:limit]
    return result


def _fallback_editorial(
    paper: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    topic = config["topics"].get(paper.get("primary_topic"), {})
    summary = re.sub(r"\s+", " ", paper.get("summary", "")).strip()
    sentences = _abstract_sentences(summary)
    brief = _join_limited(sentences[:2], 240) or "摘要信息不足，请直接阅读原文。"
    tags = paper.get("contribution_tags") or topic.get("contribution_tags", [])[:2]
    major = paper.get("content_tier") == "major"
    innovation_markers = (
        "introduce",
        "propose",
        "present",
        "method",
        "framework",
        "architecture",
        "algorithm",
        "novel",
    )
    experiment_markers = (
        "experiment",
        "result",
        "improve",
        "outperform",
        "achieve",
        "demonstrate",
        "show",
        "evaluation",
    )
    innovation_sentences = [
        sentence
        for sentence in sentences
        if any(marker in sentence.lower() for marker in innovation_markers)
    ]
    experiment_sentences = [
        sentence
        for sentence in sentences
        if any(marker in sentence.lower() for marker in experiment_markers)
    ]
    background_candidates = [
        sentence
        for sentence in sentences
        if sentence not in innovation_sentences and sentence not in experiment_sentences
    ]
    background = _join_limited(background_candidates[:3] or sentences[:2], 520)
    findings = _join_limited(experiment_sentences[-3:] or sentences[-2:], 520)
    innovation_points = innovation_sentences[:4]
    if not innovation_points:
        innovation_points = [
            paper.get("coarse_rationale") or "论文围绕核心瓶颈提出新方法。"
        ]
    return {
        "newspaper_title": f"{topic.get('name', 'AI 研究')}：{paper.get('title', 'Untitled')}"
        if major
        else paper.get("title", "Untitled"),
        "dek": brief,
        "background_and_pain": background if major else "",
        "core_innovations": innovation_points if major else [],
        "experimental_findings": findings if major else "",
        "brief_points": [] if major else [brief],
        "contribution_tags": tags,
        "editorial_model_status": "fallback_extract",
    }


def editorialize_paper(
    paper: dict[str, Any], config: dict[str, Any], client: CompletionClient
) -> dict[str, Any]:
    fallback = _fallback_editorial(paper, config)
    if not client.available:
        paper.update(fallback)
        return paper
    major = paper.get("content_tier") == "major"
    hero = bool(paper.get("is_hero"))
    if hero:
        background_requirement = "160-260 Chinese characters"
        innovation_requirement = "3-5 Chinese bullet strings, 45-90 characters each"
        findings_requirement = "160-260 Chinese characters"
        dek_requirement = "60-100 Chinese characters"
    elif major:
        background_requirement = "120-190 Chinese characters"
        innovation_requirement = "2-4 Chinese bullet strings, 35-75 characters each"
        findings_requirement = "120-190 Chinese characters"
        dek_requirement = "45-80 Chinese characters"
    else:
        background_requirement = "empty string"
        innovation_requirement = "empty array"
        findings_requirement = "empty string"
        dek_requirement = "one concise Chinese sentence"
    requirements = {
        "newspaper_title": "restrained compelling Chinese headline",
        "dek": dek_requirement,
        "background_and_pain": background_requirement,
        "core_innovations": innovation_requirement,
        "experimental_findings": findings_requirement,
        "brief_points": "empty array"
        if major
        else "2-3 bullets, about 100 Chinese characters total",
        "contribution_tags": "0-4 allowlisted tags only",
        "non_repetition": "background, innovations and findings must cover distinct information; do not paraphrase the same sentence three times",
    }
    prompt = json.dumps(
        {
            "task": "Write one Financial Times-like serious Chinese technology newspaper entry. Do not invent facts.",
            "tier": paper.get("content_tier"),
            "paper": {
                key: paper.get(key)
                for key in (
                    "title",
                    "summary",
                    "venue",
                    "venue_tags",
                    "presentation_type",
                    "citation_count",
                    "coarse_rationale",
                )
            },
            "requirements": requirements,
            "contribution_tag_allowlist": config["selection"][
                "contribution_tag_allowlist"
            ],
        },
        ensure_ascii=False,
    )
    response = client.complete(
        [
            {
                "role": "system",
                "content": "You are the senior Chinese editor of an AI research newspaper. Return JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=3000 if hero else (2000 if major else 900),
    )
    if not response:
        paper.update(fallback)
        return paper
    try:
        result = parse_json_response(response)
        innovations = result.get("core_innovations") or []
        bullets = result.get("brief_points") or []
        if not isinstance(innovations, list) or not isinstance(bullets, list):
            raise TypeError("Editorial list fields must be arrays")
        paper.update(
            {
                "newspaper_title": str(
                    result.get("newspaper_title") or fallback["newspaper_title"]
                ),
                "dek": str(result.get("dek") or fallback["dek"]),
                "background_and_pain": str(
                    result.get("background_and_pain") or fallback["background_and_pain"]
                ),
                "core_innovations": [str(item) for item in innovations[:4]],
                "experimental_findings": str(
                    result.get("experimental_findings")
                    or fallback["experimental_findings"]
                ),
                "brief_points": [str(item) for item in bullets[:3]],
                "contribution_tags": _allowed_tags(
                    result.get("contribution_tags") or [], paper, config
                ),
                "editorial_model_status": "available",
            }
        )
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        LOGGER.warning("Invalid editorial JSON; using fallback: %s", type(exc).__name__)
        paper.update(fallback)
    return paper


def explain_figure(paper: dict[str, Any], client: CompletionClient) -> dict[str, Any]:
    if paper.get("content_tier") != "major":
        paper["figure_explanation"] = ""
        return paper
    if paper.get("figure_status") != "available" or not paper.get("figure_url"):
        paper.update(
            {
                "figure_explanation": "",
                "figure_model_status": "not_available",
            }
        )
        return paper
    if not client.available:
        caption = paper.get("figure_caption", "")
        paper.update(
            {
                "figure_explanation": f"Figure 1 图注显示：{caption}"
                if caption
                else "已抓取 Figure 1；当前未配置视觉模型，暂不生成图解。",
                "figure_model_status": "fallback_caption",
            }
        )
        return paper
    messages = [
        {
            "role": "system",
            "content": "Explain AI architecture figures conservatively in Simplified Chinese. Return JSON only.",
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "task": "Explain Figure 1 in 2-3 Chinese sentences; describe information flow and claimed contribution; do not infer unreadable labels.",
                            "title": paper.get("title"),
                            "abstract": paper.get("summary"),
                            "caption": paper.get("figure_caption"),
                            "output": {"figure_explanation": "string"},
                        },
                        ensure_ascii=False,
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": paper.get("figure_source_url") or paper["figure_url"]
                    },
                },
            ],
        },
    ]
    response = client.complete(messages, max_tokens=600)
    if response:
        try:
            explanation = str(
                parse_json_response(response).get("figure_explanation", "")
            ).strip()
            if explanation:
                paper.update(
                    {
                        "figure_explanation": explanation,
                        "figure_model_status": "available",
                    }
                )
                return paper
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    caption = paper.get("figure_caption", "")
    paper.update(
        {
            "figure_explanation": f"Figure 1 图注显示：{caption}"
            if caption
            else "Figure 1 已抓取，但视觉解读暂时失败。",
            "figure_model_status": "fallback_caption",
        }
    )
    return paper


def generate_editorial_content(
    papers: list[dict[str, Any]], config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    config = config or load_config()
    client = OpenAICompatibleClient(config["llm"]["editorial"])
    for index, paper in enumerate(papers, 1):
        LOGGER.info("Editorial generation %s/%s", index, len(papers))
        editorialize_paper(paper, config, client)
        explain_figure(paper, client)
    return papers


def generate_figure_explanations(
    papers: list[dict[str, Any]], config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Generate figure explanations after final selection and image enrichment."""

    config = config or load_config()
    client = OpenAICompatibleClient(config["llm"]["editorial"])
    for paper in papers:
        explain_figure(paper, client)
    return papers


def build_report(
    papers: list[dict[str, Any]],
    target_date: date,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    selected, meta = select_daily_papers(papers, target_date, config)
    topic_id = meta["focus_topic"]
    return {
        "schema_version": 2,
        "report": {
            "date": target_date.isoformat(),
            "newspaper_name": config["project"]["newspaper_name"],
            "subtitle": config["project"]["subtitle"],
            "focus_topic": topic_id,
            "focus_topic_name": config["topics"][topic_id]["name"],
            "focus_topic_name_en": config["topics"][topic_id]["name_en"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **meta,
        },
        "papers": selected,
    }


# Backward-compatible functions.
def filter_papers_by_topic(papers: list, topic: str = "") -> list:
    del topic
    config = load_config()
    return [
        paper
        for paper in coarse_classify_papers(papers, config)
        if paper.get("is_relevant")
    ]


def rate_papers(papers: list) -> list:
    for paper in papers:
        paper["overall_priority_score"] = round(
            _clamp_score(paper.get("novelty_score"), 5) * 0.45
            + _clamp_score(paper.get("potential_impact_score"), 5) * 0.35
            + _clamp_score(paper.get("clarity_score"), 5) * 0.20,
            2,
        )
    return papers
