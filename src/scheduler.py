"""Fair topic cooldown scheduling and rotating editorial angle selection."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from config import PROJECT_ROOT
except ImportError:  # pragma: no cover
    from .config import PROJECT_ROOT


@dataclass(frozen=True)
class ScheduleDecision:
    """The public, reproducible result of one daily scheduling decision."""

    topic_id: str
    topic_name: str
    topic_name_en: str
    angle_id: str
    angle_name: str
    angle_name_en: str
    angle_instruction: str
    search_query: str
    days_unselected: int
    cooldown_readiness: float
    selection_reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_search_query(
    topic: dict[str, Any], target_date: date, lookback_days: int
) -> str:
    """Build the exact arXiv query associated with a scheduled topic."""

    categories = " OR ".join(
        f"cat:{category}" for category in topic.get("categories", [])
    )
    keyword_clauses: list[str] = []
    for keyword in topic.get("keywords", []):
        escaped = str(keyword).replace('"', r"\"")
        keyword_clauses.extend((f'ti:"{escaped}"', f'abs:"{escaped}"'))
    keywords = " OR ".join(keyword_clauses)
    start = datetime.combine(
        target_date - timedelta(days=max(lookback_days - 1, 0)),
        time.min,
        tzinfo=timezone.utc,
    )
    end = datetime.combine(
        target_date + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    date_range = f"submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]"
    category_clause = f"({categories}) AND " if categories else ""
    return f"{category_clause}({keywords}) AND {date_range}"


def _report_files(root: Path, config: dict[str, Any]) -> Iterable[Path]:
    json_dir = root / str(config["render"]["json_dir"])
    if json_dir.is_dir():
        yield from json_dir.rglob("*.json")


def load_schedule_history(
    config: dict[str, Any],
    target_date: date,
    *,
    project_root: Path | None = None,
) -> list[dict[str, str]]:
    """Read only public report metadata; malformed and legacy reports are ignored."""

    root = project_root or PROJECT_ROOT
    history: list[dict[str, str]] = []
    for path in _report_files(root, config):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            report = payload.get("report", {}) if isinstance(payload, dict) else {}
            report_date = date.fromisoformat(str(report.get("date", ""))[:10])
            topic_id = str(report.get("focus_topic") or "")
            angle_id = str(report.get("angle_id") or "")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if report_date >= target_date or not topic_id:
            continue
        item = {"date": report_date.isoformat(), "topic_id": topic_id}
        if angle_id:
            item["angle_id"] = angle_id
        history.append(item)
    return sorted(history, key=lambda item: item["date"])


def _latest_dates(history: Iterable[dict[str, str]], id_field: str) -> dict[str, date]:
    latest: dict[str, date] = {}
    for item in history:
        identifier = str(item.get(id_field) or "")
        if not identifier:
            continue
        try:
            item_date = date.fromisoformat(str(item["date"])[:10])
        except (KeyError, ValueError):
            continue
        if identifier not in latest or item_date > latest[identifier]:
            latest[identifier] = item_date
    return latest


def _deterministic_seed(
    target_date: date, namespace: str, latest: dict[str, date]
) -> int:
    state = "|".join(
        f"{identifier}:{item_date.isoformat()}"
        for identifier, item_date in sorted(latest.items())
    )
    digest = hashlib.sha256(
        f"{namespace}|{target_date.isoformat()}|{state}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big")


def _choose_fair_identifier(
    identifiers: list[str],
    latest: dict[str, date],
    target_date: date,
    *,
    cooldown_days: float,
    starvation_days: int,
    unseen_days: int,
    namespace: str,
) -> tuple[str, int, float, str]:
    if not identifiers:
        raise ValueError("At least one schedulable identifier is required")
    ages = {
        identifier: max(
            (target_date - latest[identifier]).days
            if identifier in latest
            else unseen_days,
            0,
        )
        for identifier in identifiers
    }
    starving = [
        identifier for identifier in identifiers if ages[identifier] >= starvation_days
    ]
    rng = random.Random(_deterministic_seed(target_date, namespace, latest))
    if starving:
        oldest_age = max(ages[identifier] for identifier in starving)
        tied = sorted(
            identifier for identifier in starving if ages[identifier] == oldest_age
        )
        chosen = tied[rng.randrange(len(tied))]
        reason = "starvation_guard"
    else:
        readiness = {
            identifier: max(
                1.0 - math.exp(-ages[identifier] / max(cooldown_days, 0.01)),
                0.0001,
            )
            for identifier in identifiers
        }
        chosen = rng.choices(
            sorted(identifiers),
            weights=[readiness[identifier] for identifier in sorted(identifiers)],
            k=1,
        )[0]
        reason = "cooldown_weighted_rotation"
    readiness_value = 1.0 - math.exp(-ages[chosen] / max(cooldown_days, 0.01))
    return chosen, ages[chosen], round(readiness_value, 4), reason


def schedule_daily_focus(
    target_date: date,
    config: dict[str, Any],
    *,
    history: list[dict[str, str]] | None = None,
    project_root: Path | None = None,
) -> ScheduleDecision:
    """Select a topic and angle without using paper-volume popularity signals."""

    scheduler = config["scheduler"]
    history = (
        history
        if history is not None
        else load_schedule_history(config, target_date, project_root=project_root)
    )
    topic_ids = [str(value) for value in scheduler.get("topic_pool", [])]
    topic_latest = _latest_dates(history, "topic_id")
    topic_id, days_unselected, readiness, reason = _choose_fair_identifier(
        topic_ids,
        topic_latest,
        target_date,
        cooldown_days=float(scheduler.get("cooldown_days", 7)),
        starvation_days=int(scheduler.get("starvation_days", 21)),
        unseen_days=int(scheduler.get("unseen_topic_days", 365)),
        namespace="topic",
    )
    angles = scheduler["angles"]
    angle_ids = [str(value) for value in scheduler.get("angle_pool", angles)]
    angle_latest = _latest_dates(history, "angle_id")
    angle_id, _, _, _ = _choose_fair_identifier(
        angle_ids,
        angle_latest,
        target_date,
        cooldown_days=float(scheduler.get("angle_cooldown_days", 4)),
        starvation_days=int(scheduler.get("angle_starvation_days", 12)),
        unseen_days=int(scheduler.get("unseen_angle_days", 365)),
        namespace="angle",
    )
    topic = config["topics"][topic_id]
    angle = angles[angle_id]
    query = build_search_query(
        topic, target_date, int(config["sources"]["arxiv"].get("lookback_days", 3))
    )
    return ScheduleDecision(
        topic_id=topic_id,
        topic_name=str(topic["name"]),
        topic_name_en=str(topic["name_en"]),
        angle_id=angle_id,
        angle_name=str(angle["name"]),
        angle_name_en=str(angle["name_en"]),
        angle_instruction=str(angle["instruction"]),
        search_query=query,
        days_unselected=days_unselected,
        cooldown_readiness=readiness,
        selection_reason=reason,
    )


def topic_query_order(decision: ScheduleDecision, config: dict[str, Any]) -> list[str]:
    """Put today's topic first while retaining cross-topic discovery."""

    configured = [str(item) for item in config["scheduler"]["topic_pool"]]
    return [
        decision.topic_id,
        *[item for item in configured if item != decision.topic_id],
    ]
