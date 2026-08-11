"""Application configuration loading and validation."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def load_local_environment(path: str | os.PathLike[str] | None = None) -> bool:
    """Load project-local secrets without overriding process/CI variables."""

    env_path = Path(path) if path else DEFAULT_ENV_PATH
    if not env_path.is_absolute():
        env_path = PROJECT_ROOT / env_path
    return load_dotenv(env_path, override=False)


load_local_environment()


class ConfigError(ValueError):
    """Raised when the newspaper configuration is incomplete or inconsistent."""


def _expand_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if isinstance(value, str):
        return os.path.expandvars(value)
    return value


def environment_value_from(
    section: dict[str, Any], field: str, default: Any = None
) -> Any:
    """Resolve a non-secret setting from its configured environment reference."""

    env_name = section.get(f"{field}_env")
    if env_name:
        value = os.getenv(str(env_name))
        if value is not None and value.strip():
            return value.strip()
    value = section.get(field, default)
    return value.strip() if isinstance(value, str) else value


def validate_config(config: dict[str, Any]) -> None:
    topic_definitions = config.get("topics", {})
    if not isinstance(topic_definitions, dict) or not topic_definitions:
        raise ConfigError("At least one topic definition is required")
    topics = set(topic_definitions)
    for topic_id, topic in topic_definitions.items():
        if not isinstance(topic, dict):
            raise ConfigError(f"Topic {topic_id} must be a mapping")
        if not topic.get("name") or not topic.get("name_en"):
            raise ConfigError(f"Topic {topic_id} must define name and name_en")
        if not isinstance(topic.get("keywords"), list) or not topic["keywords"]:
            raise ConfigError(f"Topic {topic_id} must define at least one keyword")
        topic.setdefault("categories", [])
        topic.setdefault("concepts", [])

    project = config.get("project", {})
    edition_size = int(project.get("edition_size", 0))
    focus_count = int(project.get("focus_count", 0))
    cross_count = int(project.get("cross_topic_count", 0))
    if edition_size <= 0 or focus_count < 0 or cross_count < 0:
        raise ConfigError("Edition size and topic allocation must be positive integers")
    if focus_count + cross_count != edition_size:
        raise ConfigError("focus_count + cross_topic_count must equal edition_size")

    rotation = config.get("topic_rotation", {})
    weekdays = {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
    if set(rotation) != weekdays:
        raise ConfigError("topic_rotation must define every weekday exactly once")
    unknown_rotation_topics = set(rotation.values()) - topics
    if unknown_rotation_topics:
        raise ConfigError(
            f"Rotation references unknown topics: {', '.join(sorted(unknown_rotation_topics))}"
        )

    scheduler = config.get("scheduler", {})
    topic_pool = scheduler.get("topic_pool", list(topic_definitions))
    if not isinstance(topic_pool, list) or not topic_pool:
        raise ConfigError("scheduler.topic_pool must contain at least one topic")
    unknown_scheduled_topics = set(topic_pool) - topics
    if unknown_scheduled_topics:
        # Older callers commonly deepcopy the configuration and replace only
        # ``topics`` and ``topic_rotation``. Keep that supported without
        # allowing an unusable scheduler to escape validation.
        rotation_topics = set(rotation.values())
        if rotation_topics and rotation_topics <= topics and rotation_topics == topics:
            scheduler["topic_pool"] = list(topic_definitions)
            topic_pool = scheduler["topic_pool"]
        else:
            raise ConfigError(
                "Scheduler references unknown topics: "
                + ", ".join(sorted(unknown_scheduled_topics))
            )
    angles = scheduler.get("angles", {})
    angle_pool = scheduler.get("angle_pool", list(angles))
    if not isinstance(angles, dict) or not angles:
        raise ConfigError("scheduler.angles must define at least one angle")
    if not isinstance(angle_pool, list) or not angle_pool:
        raise ConfigError("scheduler.angle_pool must contain at least one angle")
    unknown_angles = set(angle_pool) - set(angles)
    if unknown_angles:
        raise ConfigError(
            f"Scheduler references unknown angles: {', '.join(sorted(unknown_angles))}"
        )
    for angle_id, angle in angles.items():
        if not isinstance(angle, dict) or not all(
            angle.get(field) for field in ("name", "name_en", "instruction")
        ):
            raise ConfigError(
                f"Angle {angle_id} must define name, name_en and instruction"
            )

    policy = config.get("editorial_policy", {})
    minimum = int(policy.get("min_selected_papers", 0))
    maximum = int(policy.get("max_selected_papers", 0))
    shortlist = int(policy.get("candidate_shortlist_size", 0))
    if minimum <= 0 or maximum < minimum or shortlist < maximum:
        raise ConfigError(
            "editorial_policy requires 0 < min <= max <= candidate_shortlist_size"
        )

    llm = config.get("llm", {})
    for role in ("coarse", "editorial"):
        section = llm.get(role)
        if not isinstance(section, dict):
            raise ConfigError(f"llm.{role} must be a mapping")
        for field in ("api_key_env", "base_url_env", "model_env"):
            env_name = str(section.get(field, "")).strip()
            if not ENVIRONMENT_NAME_PATTERN.fullmatch(env_name):
                raise ConfigError(
                    f"llm.{role}.{field} must reference an uppercase environment variable"
                )
        for field in (
            "token_field_env",
            "reasoning_format_env",
            "reasoning_effort_env",
        ):
            env_name = str(section.get(field, "")).strip()
            if env_name and not ENVIRONMENT_NAME_PATTERN.fullmatch(env_name):
                raise ConfigError(
                    f"llm.{role}.{field} must reference an uppercase environment variable"
                )

    memory = config.get("memory", {})
    if memory.get("provider") not in {"github_gist", "disabled"}:
        raise ConfigError("memory.provider must be github_gist or disabled")
    if memory.get("provider") == "github_gist" and not memory.get("filename"):
        raise ConfigError("memory.filename is required for GitHub Gist storage")

    render = config.get("render", {})
    for field in ("output_dir", "json_dir", "public_stylesheet"):
        raw_value = str(render.get(field, "")).strip()
        value = Path(raw_value)
        if (
            not raw_value
            or value == Path(".")
            or value.is_absolute()
            or ".." in value.parts
        ):
            raise ConfigError(f"render.{field} must stay inside the project root")
    for field in ("template", "stylesheet"):
        raw_value = str(render.get(field, "")).strip()
        value = Path(raw_value)
        if (
            not raw_value
            or value == Path(".")
            or value.is_absolute()
            or ".." in value.parts
        ):
            raise ConfigError(f"render.{field} must stay inside templates/")
    raw_cache_dir = str(
        config.get("sources", {}).get("arxiv_html", {}).get("cache_dir", "")
    ).strip()
    cache_dir = Path(raw_cache_dir)
    if (
        not raw_cache_dir
        or cache_dir == Path(".")
        or cache_dir.is_absolute()
        or ".." in cache_dir.parts
    ):
        raise ConfigError(
            "sources.arxiv_html.cache_dir must stay inside the project root"
        )


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load the YAML configuration and return an isolated mutable dictionary."""

    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    config = _expand_environment(deepcopy(loaded))
    validate_config(config)
    config["_meta"] = {
        "project_root": str(PROJECT_ROOT),
        "config_path": str(config_path.resolve()),
    }
    return config


def secret_from(section: dict[str, Any]) -> str | None:
    """Resolve a secret referenced by an ``api_key_env`` configuration field."""

    env_name = section.get("api_key_env")
    value = os.getenv(str(env_name)) if env_name else None
    return value.strip() if value and value.strip() else None
