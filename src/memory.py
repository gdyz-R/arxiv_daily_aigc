"""Private concept-ledger storage with a graceful GitHub Gist fallback."""

from __future__ import annotations

import json
import logging
import os
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol

import requests

LOGGER = logging.getLogger(__name__)
VALID_STATUSES = {"learning", "mastered"}


class MemoryCodec(Protocol):
    """Codec seam reserved for a future client-side AES-GCM implementation."""

    name: str

    def encode(self, ledger: dict[str, Any]) -> str: ...

    def decode(self, content: str) -> dict[str, Any]: ...


class RequestSession(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response: ...


class PlaintextJsonCodec:
    """Current codec: readable JSON in a secret (unlisted) Gist."""

    name = "plaintext-json"

    def encode(self, ledger: dict[str, Any]) -> str:
        return json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True)

    def decode(self, content: str) -> dict[str, Any]:
        value = json.loads(content)
        if not isinstance(value, dict):
            raise TypeError("Concept ledger must be a JSON object")
        return value


class UnsupportedMemoryCodec:
    """Fail closed so a future encryption setting never writes plaintext."""

    def __init__(self, name: str):
        self.name = name

    def encode(self, ledger: dict[str, Any]) -> str:
        del ledger
        raise ValueError(f"Unsupported memory codec: {self.name}")

    def decode(self, content: str) -> dict[str, Any]:
        del content
        raise ValueError(f"Unsupported memory codec: {self.name}")


@dataclass(frozen=True)
class MemoryReadResult:
    ledger: dict[str, Any]
    status: str
    used_remote: bool = False


@dataclass(frozen=True)
class MemoryWriteResult:
    status: str
    updated: bool = False


def empty_ledger() -> dict[str, Any]:
    return {"schema_version": 1, "updated_at": None, "concepts": {}}


def normalize_ledger(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return empty_ledger()
    concepts = value.get("concepts")
    if not isinstance(concepts, dict):
        concepts = {}
    normalized = empty_ledger()
    normalized["updated_at"] = value.get("updated_at")
    for concept_id, raw in concepts.items():
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "learning")
        if status not in VALID_STATUSES:
            status = "learning"
        try:
            mastery = min(max(float(raw.get("mastery_level", 0.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            mastery = 0.0
        normalized["concepts"][str(concept_id)] = {
            "name": str(raw.get("name") or concept_id)[:120],
            "status": status,
            "mastery_level": round(mastery, 3),
            "first_learned_at": raw.get("first_learned_at"),
            "last_reviewed_at": raw.get("last_reviewed_at"),
            "mastery_summary": str(raw.get("mastery_summary") or "")[:600],
            "source_reports": [
                str(item)[:10]
                for item in raw.get("source_reports", [])
                if isinstance(item, str)
            ][-20:],
        }
    return normalized


def _codec_from_config(section: dict[str, Any]) -> MemoryCodec:
    codec_name = str(section.get("codec", "plaintext-json"))
    if codec_name != "plaintext-json":
        LOGGER.warning(
            "Unsupported memory codec %s; failing closed to empty-memory mode",
            codec_name,
        )
        return UnsupportedMemoryCodec(codec_name)
    return PlaintextJsonCodec()


class GitHubGistMemoryClient:
    """Small Gist REST client that never makes the newspaper pipeline fail."""

    def __init__(
        self,
        section: dict[str, Any],
        session: RequestSession | None = None,
        codec: MemoryCodec | None = None,
    ):
        self.section = section
        self.gist_id = os.getenv(str(section.get("gist_id_env", "GIST_ID")), "")
        self.token = os.getenv(str(section.get("token_env", "GIST_TOKEN")), "")
        self.filename = str(section.get("filename", "concept_ledger.json"))
        self.base_url = str(section.get("base_url", "https://api.github.com")).rstrip(
            "/"
        )
        self.session = session or requests.Session()
        self.codec = codec or _codec_from_config(section)

    @property
    def available(self) -> bool:
        return bool(
            self.gist_id and self.token and isinstance(self.codec, PlaintextJsonCodec)
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Daily-AI-Research-Gazette/3.0",
        }

    def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response | None:
        retries = max(int(self.section.get("max_retries", 2)), 1)
        timeout = float(self.section.get("timeout_seconds", 15))
        for attempt in range(retries):
            try:
                response = self.session.request(
                    method, url, headers=self.headers, timeout=timeout, **kwargs
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 < retries:
                        time.sleep(min(2**attempt, 5))
                        continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                LOGGER.warning(
                    "Gist memory %s attempt %s/%s failed: %s",
                    method,
                    attempt + 1,
                    retries,
                    type(exc).__name__,
                )
                if attempt + 1 < retries:
                    time.sleep(min(2**attempt, 5))
        return None

    def read(self) -> MemoryReadResult:
        if not self.available:
            status = (
                "empty_unsupported_codec"
                if not isinstance(self.codec, PlaintextJsonCodec)
                else "empty_unconfigured"
            )
            return MemoryReadResult(empty_ledger(), status)
        response = self._request("GET", f"{self.base_url}/gists/{self.gist_id}")
        if response is None:
            return MemoryReadResult(empty_ledger(), "empty_remote_failure")
        try:
            file_info = response.json().get("files", {}).get(self.filename)
            if not isinstance(file_info, dict):
                return MemoryReadResult(empty_ledger(), "empty_file_missing", True)
            content = file_info.get("content")
            if file_info.get("truncated") and file_info.get("raw_url"):
                raw_response = self._request("GET", str(file_info["raw_url"]))
                content = raw_response.text if raw_response is not None else None
            if not isinstance(content, str):
                return MemoryReadResult(empty_ledger(), "empty_content_missing", True)
            return MemoryReadResult(
                normalize_ledger(self.codec.decode(content)), "available", True
            )
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            LOGGER.warning(
                "Invalid concept ledger; using empty memory: %s", type(exc).__name__
            )
            return MemoryReadResult(empty_ledger(), "empty_invalid_ledger", True)

    def write(self, ledger: dict[str, Any]) -> MemoryWriteResult:
        if not self.available:
            status = (
                "skipped_unsupported_codec"
                if not isinstance(self.codec, PlaintextJsonCodec)
                else "skipped_unconfigured"
            )
            return MemoryWriteResult(status)
        normalized = normalize_ledger(ledger)
        normalized["updated_at"] = datetime.now(timezone.utc).isoformat()
        response = self._request(
            "PATCH",
            f"{self.base_url}/gists/{self.gist_id}",
            json={"files": {self.filename: {"content": self.codec.encode(normalized)}}},
        )
        return (
            MemoryWriteResult("updated", True)
            if response is not None
            else MemoryWriteResult("skipped_remote_failure")
        )


def relevant_memory_context(
    ledger: dict[str, Any], concept_ids: list[str], *, limit: int = 12
) -> list[dict[str, Any]]:
    concepts = normalize_ledger(ledger)["concepts"]
    context: list[dict[str, Any]] = []
    for concept_id in concept_ids[:limit]:
        entry = concepts.get(concept_id)
        if entry:
            context.append({"concept_id": concept_id, **deepcopy(entry)})
        else:
            context.append(
                {
                    "concept_id": concept_id,
                    "name": concept_id.replace("_", " ").title(),
                    "status": "first_contact",
                    "mastery_level": 0.0,
                    "mastery_summary": "",
                }
            )
    return context


def merge_concept_updates(
    ledger: dict[str, Any], payload: Any, report_date: date
) -> tuple[dict[str, Any], int]:
    """Validate and merge model-produced updates without trusting its full document."""

    merged = normalize_ledger(ledger)
    if not isinstance(payload, dict):
        return merged, 0
    updates = payload.get("concept_updates", [])
    if not isinstance(updates, list):
        return merged, 0
    updated_count = 0
    for raw in updates[:20]:
        if not isinstance(raw, dict):
            continue
        concept_id = str(raw.get("concept_id") or "").strip().lower()
        concept_id = "_".join(
            part for part in concept_id.replace("-", "_").split("_") if part
        )
        if not concept_id or len(concept_id) > 80:
            continue
        status = str(raw.get("status") or "learning")
        if status not in VALID_STATUSES:
            continue
        try:
            mastery = min(max(float(raw.get("mastery_level", 0.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            continue
        summary = str(raw.get("mastery_summary") or "").strip()[:600]
        if not summary:
            continue
        existing = merged["concepts"].get(concept_id, {})
        report_day = report_date.isoformat()
        sources = [
            str(item)
            for item in existing.get("source_reports", [])
            if isinstance(item, str)
        ]
        if report_day not in sources:
            sources.append(report_day)
        merged["concepts"][concept_id] = {
            "name": str(raw.get("name") or existing.get("name") or concept_id)[:120],
            "status": status,
            "mastery_level": round(mastery, 3),
            "first_learned_at": existing.get("first_learned_at") or report_day,
            "last_reviewed_at": report_day,
            "mastery_summary": summary,
            "source_reports": sources[-20:],
        }
        updated_count += 1
    return merged, updated_count
