"""Deterministic prominence annotations and edition-level policy checks."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def _published_date(paper: dict[str, Any]) -> date | None:
    raw = str(paper.get("published_date") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def annotate_well_known_paper(
    paper: dict[str, Any], target_date: date, config: dict[str, Any]
) -> dict[str, Any]:
    """Attach public, reproducible evidence for top-venue or citation prominence."""

    configured_venues = {
        str(tag) for tag in config["selection"].get("well_known_venues", [])
    }
    venue_tags = [
        str(tag) for tag in paper.get("venue_tags", []) if str(tag) in configured_venues
    ]
    citations = max(int(paper.get("citation_count") or 0), 0)
    threshold = int(config["selection"].get("high_citation_threshold", 100))
    reasons = [f"top_venue:{tag}" for tag in venue_tags]
    if citations >= threshold:
        reasons.append(f"high_citation:{citations}")
    published = _published_date(paper)
    age_days = max((target_date - published).days, 0) if published else 0
    paper["well_known"] = bool(reasons)
    paper["well_known_reasons"] = reasons
    paper["historical_anchor"] = bool(reasons) and age_days > int(
        config["selection"].get("recent_days", 120)
    )
    return paper


def paper_matches_focus(paper: dict[str, Any], focus_topic: str) -> bool:
    """Return whether a classified or raw candidate belongs to the focus domain."""

    primary_topic = str(paper.get("primary_topic") or "")
    if primary_topic:
        return primary_topic == focus_topic
    return focus_topic in set(paper.get("candidate_topics", []))


def is_focus_well_known(paper: dict[str, Any], focus_topic: str) -> bool:
    return bool(paper.get("well_known")) and paper_matches_focus(paper, focus_topic)


def prominence_summary(
    papers: list[dict[str, Any]], focus_topic: str
) -> dict[str, int]:
    focus_well_known = [
        paper for paper in papers if is_focus_well_known(paper, focus_topic)
    ]
    return {
        "well_known_paper_count": len(focus_well_known),
        "historical_anchor_count": sum(
            bool(paper.get("historical_anchor")) for paper in focus_well_known
        ),
    }


def prominence_policy_errors(
    papers: list[dict[str, Any]], focus_topic: str, config: dict[str, Any]
) -> list[str]:
    """Validate the 1-3 focus-domain prominence quota and historical anchor."""

    summary = prominence_summary(papers, focus_topic)
    minimum = int(config["selection"].get("min_well_known_papers", 1))
    maximum = int(config["selection"].get("max_well_known_papers", 3))
    errors: list[str] = []
    count = summary["well_known_paper_count"]
    if count < minimum:
        errors.append(f"requires at least {minimum} focus-domain well-known paper(s)")
    if count > maximum:
        errors.append(f"allows at most {maximum} focus-domain well-known paper(s)")
    if summary["historical_anchor_count"] < 1:
        errors.append("requires at least one well-known paper older than recent_days")
    return errors
