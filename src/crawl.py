"""Multi-source paper crawling and metadata enrichment.

The module deliberately keeps network adapters independent so they can fail or be
tested in isolation.  arXiv remains the high-frequency source, while official
NeurIPS pages provide trustworthy venue and presentation labels.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from collections.abc import Iterable
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from datetime import time as datetime_time
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

import arxiv
import requests
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

try:
    from archive import report_slug
    from config import load_config, secret_from
    from prominence import annotate_well_known_paper
    from scheduler import build_search_query
except ImportError:  # pragma: no cover - package execution
    from .archive import report_slug
    from .config import load_config, secret_from
    from .prominence import annotate_well_known_paper
    from .scheduler import build_search_query


LOGGER = logging.getLogger(__name__)
ARXIV_ID_PATTERN = re.compile(
    r"(?:(?:abs|pdf|html)/)?(?P<id>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def html_attribute(tag: Any, name: str) -> str:
    """Return a BeautifulSoup attribute as a scalar string."""

    value = tag.get(name)
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def extract_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    match = ARXIV_ID_PATTERN.search(value)
    return match.group("id") if match else None


def paper_identity(paper: dict[str, Any]) -> str:
    arxiv_id = paper.get("arxiv_id") or extract_arxiv_id(paper.get("url"))
    return (
        f"arxiv:{arxiv_id.lower()}"
        if arxiv_id
        else f"title:{normalize_title(paper.get('title', ''))}"
    )


def build_arxiv_query(
    topic: dict[str, Any], target_date: date, lookback_days: int
) -> str:
    """Backward-compatible alias for the scheduler-owned exact query builder."""

    return build_search_query(topic, target_date, lookback_days)


def _iso_datetime(value: datetime | date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime.combine(
            value, datetime_time.min, tzinfo=timezone.utc
        ).isoformat()
    return str(value)


def _contains_topic_keyword(title: str, topics: dict[str, Any]) -> list[str]:
    normalized = title.lower()
    return [
        topic_id
        for topic_id, topic in topics.items()
        if any(keyword.lower() in normalized for keyword in topic.get("keywords", []))
    ]


def _matching_topics(
    title: str,
    summary: str,
    categories: Iterable[str],
    topics: dict[str, Any],
) -> list[str]:
    """Match the same category-and-keyword intent used by the arXiv API query."""

    searchable = f"{title} {summary}".lower()
    paper_categories = set(categories)
    return [
        topic_id
        for topic_id, topic in topics.items()
        if (
            not topic.get("categories")
            or bool(paper_categories & set(topic.get("categories", [])))
        )
        and any(
            str(keyword).lower() in searchable for keyword in topic.get("keywords", [])
        )
    ]


class _TimeoutSession(requests.Session):
    """Requests session that applies a default timeout to library-owned calls."""

    def __init__(self, timeout_seconds: float):
        super().__init__()
        self.timeout_seconds = timeout_seconds

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout_seconds)
        return super().request(method, url, **kwargs)


class ArxivRSSSource:
    """Fallback adapter for arXiv category RSS feeds.

    RSS remains available when the query API rate-limits shared CI addresses.
    The broad category feeds are filtered locally with the configured topic
    categories and keywords, preserving the intent of :func:`build_arxiv_query`.
    """

    ARXIV_NAMESPACE = "http://arxiv.org/schemas/atom"
    DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"

    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        self.app_config = config
        self.config = config["sources"]["arxiv"]
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config["sources"]["user_agent"]})

    @staticmethod
    def _clean_summary(description: str) -> str:
        text = BeautifulSoup(description or "", "html.parser").get_text(" ", strip=True)
        text = re.sub(
            r"^arXiv:\S+\s+Announce Type:\s*\S+\s+Abstract:\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return re.sub(r"\s+", " ", text).strip()

    def _paper_from_item(
        self, item: ElementTree.Element, feed_category: str, target_date: date
    ) -> dict[str, Any] | None:
        title = str(item.findtext("title") or "").strip()
        link = str(item.findtext("link") or "").strip()
        arxiv_id = extract_arxiv_id(link or item.findtext("guid"))
        if not title or not arxiv_id:
            return None
        summary = self._clean_summary(str(item.findtext("description") or ""))
        categories = sorted(
            {
                feed_category,
                *(
                    str(category.text).strip()
                    for category in item.findall("category")
                    if category.text
                ),
            }
        )
        announce_type = str(
            item.findtext(f"{{{self.ARXIV_NAMESPACE}}}announce_type") or "new"
        ).lower()
        allowed_types = {
            str(value).lower()
            for value in self.config.get("rss_announce_types", ["new", "cross"])
        }
        if announce_type not in allowed_types:
            return None
        try:
            published = parsedate_to_datetime(str(item.findtext("pubDate") or ""))
            if not published.tzinfo:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
        lookback_days = int(self.config.get("lookback_days", 3))
        start_date = target_date - timedelta(days=max(lookback_days - 1, 0))
        if not start_date <= published.date() <= target_date:
            return None
        topic_ids = _matching_topics(
            title, summary, categories, self.app_config["topics"]
        )
        if not topic_ids:
            return None
        creators = str(item.findtext(f"{{{self.DC_NAMESPACE}}}creator") or "").strip()
        return {
            "paper_id": f"arxiv:{arxiv_id}",
            "source": "arxiv_rss",
            "sources": ["arxiv_rss"],
            "source_id": arxiv_id,
            "arxiv_id": arxiv_id,
            "title": title,
            "summary": summary,
            "url": link or f"https://arxiv.org/abs/{arxiv_id}",
            "abstract_url": link or f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            "published_date": published.astimezone(timezone.utc).isoformat(),
            "updated_date": None,
            "categories": categories,
            "authors": [
                name.strip() for name in re.split(r",| and ", creators) if name.strip()
            ],
            "candidate_topics": topic_ids,
            "citation_count": 0,
            "venue": "",
            "venue_tags": [],
            "presentation_type": "",
            "figure_url": None,
            "figure_caption": "",
            "figure_status": "not_requested",
            "rss_announce_type": announce_type,
        }

    def fetch_with_diagnostics(
        self, target_date: date
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        configured_categories = sorted(
            {
                str(category)
                for topic in self.app_config["topics"].values()
                for category in topic.get("categories", [])
            }
        )
        diagnostics: dict[str, Any] = {
            "status": "empty",
            "feed_count": len(configured_categories),
            "feeds_succeeded": 0,
            "feeds_failed": 0,
            "parsed_item_count": 0,
            "result_count": 0,
            "errors": [],
        }
        papers: dict[str, dict[str, Any]] = {}
        per_topic_counts = {topic_id: 0 for topic_id in self.app_config["topics"]}
        limit = min(
            int(self.config.get("max_results_per_topic", 45)),
            int(self.app_config["selection"].get("candidate_limit_per_topic", 45)),
        )
        base_url = str(
            self.config.get("rss_base_url", "https://rss.arxiv.org/rss")
        ).rstrip("/")
        for category in configured_categories:
            url = f"{base_url}/{category}"
            try:
                response = self.session.get(
                    url, timeout=float(self.config.get("timeout_seconds", 30))
                )
                response.raise_for_status()
                root = ElementTree.fromstring(response.content)
                diagnostics["feeds_succeeded"] += 1
            except (
                requests.RequestException,
                ElementTree.ParseError,
                ValueError,
            ) as exc:
                diagnostics["feeds_failed"] += 1
                diagnostics["errors"].append(
                    {"category": category, "type": type(exc).__name__}
                )
                LOGGER.warning(
                    "arXiv RSS category=%s failed: %s",
                    category,
                    type(exc).__name__,
                )
                continue
            channel = root.find("channel")
            if channel is None:
                diagnostics["feeds_failed"] += 1
                diagnostics["errors"].append(
                    {"category": category, "type": "MissingChannel"}
                )
                continue
            for item in channel.findall("item"):
                diagnostics["parsed_item_count"] += 1
                paper = self._paper_from_item(item, category, target_date)
                if not paper:
                    continue
                available_topics = [
                    topic_id
                    for topic_id in paper["candidate_topics"]
                    if per_topic_counts[topic_id] < limit
                ]
                if not available_topics:
                    continue
                paper["candidate_topics"] = available_topics
                identity = paper_identity(paper)
                if identity in papers:
                    current = papers[identity]
                    new_topics = set(available_topics) - set(
                        current["candidate_topics"]
                    )
                    current["candidate_topics"] = sorted(
                        set(current["candidate_topics"]) | set(available_topics)
                    )
                    current["categories"] = sorted(
                        set(current["categories"]) | set(paper["categories"])
                    )
                else:
                    papers[identity] = paper
                    new_topics = set(available_topics)
                for topic_id in new_topics:
                    per_topic_counts[topic_id] += 1
        result = list(papers.values())
        diagnostics["result_count"] = len(result)
        if result:
            diagnostics["status"] = "available"
        elif diagnostics["feeds_succeeded"] == 0:
            diagnostics["status"] = "failed"
        return result, diagnostics

    def fetch(self, target_date: date) -> list[dict[str, Any]]:
        papers, _ = self.fetch_with_diagnostics(target_date)
        return papers


class ArxivSource:
    def __init__(
        self,
        config: dict[str, Any],
        client: Any | None = None,
        rss_source: ArxivRSSSource | None = None,
    ):
        self.config = config
        source_config = config["sources"]["arxiv"]
        self.source_config = source_config
        self.client = client or arxiv.Client(
            delay_seconds=float(source_config.get("delay_seconds", 3)),
            # Retry here so 429 can trip the RSS circuit breaker immediately.
            num_retries=0,
        )
        if client is None and hasattr(self.client, "_session"):
            self.client._session = _TimeoutSession(
                float(source_config.get("timeout_seconds", 30))
            )
        self.rss_source = rss_source or ArxivRSSSource(config)

    @staticmethod
    def _paper_from_result(result: Any, topic_id: str) -> dict[str, Any]:
        arxiv_id = extract_arxiv_id(result.entry_id)
        return {
            "paper_id": f"arxiv:{arxiv_id}" if arxiv_id else result.entry_id,
            "source": "arxiv",
            "sources": ["arxiv"],
            "source_id": arxiv_id or result.entry_id,
            "arxiv_id": arxiv_id,
            "title": result.title.strip(),
            "summary": result.summary.strip(),
            "url": result.entry_id,
            "abstract_url": result.entry_id,
            "pdf_url": result.pdf_url,
            "published_date": _iso_datetime(result.published),
            "updated_date": _iso_datetime(result.updated),
            "categories": list(result.categories),
            "authors": [author.name for author in result.authors],
            "candidate_topics": [topic_id],
            "citation_count": 0,
            "venue": "",
            "venue_tags": [],
            "presentation_type": "",
            "figure_url": None,
            "figure_caption": "",
            "figure_status": "not_requested",
        }

    def fetch_with_diagnostics(
        self, target_date: date
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        papers: dict[str, dict[str, Any]] = {}
        diagnostics: dict[str, Any] = {
            "status": "empty",
            "api_topics_attempted": 0,
            "api_topics_succeeded": 0,
            "api_topics_failed": 0,
            "api_result_count": 0,
            "fallback_used": False,
            "result_count": 0,
            "errors": [],
        }
        limit = min(
            int(self.source_config.get("max_results_per_topic", 45)),
            int(self.config["selection"].get("candidate_limit_per_topic", 45)),
        )
        fallback_reason = ""
        for topic_id, topic in self.config["topics"].items():
            query = build_arxiv_query(
                topic,
                target_date,
                int(self.source_config.get("lookback_days", 3)),
            )
            LOGGER.info("arXiv topic=%s", topic_id)
            search = arxiv.Search(
                query=query,
                max_results=limit,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Descending,
            )
            diagnostics["api_topics_attempted"] += 1
            attempts = max(int(self.source_config.get("num_retries", 1)), 1)
            for attempt in range(attempts):
                try:
                    topic_results = list(self.client.results(search))
                    diagnostics["api_topics_succeeded"] += 1
                    diagnostics["api_result_count"] += len(topic_results)
                    for result in topic_results:
                        paper = self._paper_from_result(result, topic_id)
                        identity = paper_identity(paper)
                        if identity in papers:
                            current = papers[identity]
                            current["candidate_topics"] = sorted(
                                set(current["candidate_topics"]) | {topic_id}
                            )
                        else:
                            papers[identity] = paper
                    break
                except Exception as exc:  # arxiv exposes transport errors by version
                    status = getattr(exc, "status", None)
                    terminal = status == 429 or isinstance(
                        exc,
                        (
                            requests.Timeout,
                            requests.ConnectionError,
                        ),
                    )
                    if not terminal and attempt + 1 < attempts:
                        time.sleep(min(2**attempt, 10))
                        continue
                    diagnostics["api_topics_failed"] += 1
                    error: dict[str, Any] = {
                        "topic": topic_id,
                        "type": type(exc).__name__,
                    }
                    if status is not None:
                        error["http_status"] = int(status)
                    diagnostics["errors"].append(error)
                    LOGGER.warning(
                        "arXiv API topic=%s failed type=%s status=%s; switching to RSS",
                        topic_id,
                        type(exc).__name__,
                        status or "n/a",
                    )
                    fallback_reason = (
                        f"http_{status}" if status is not None else type(exc).__name__
                    )
                    break
            if fallback_reason:
                break

        if (fallback_reason or not papers) and self.source_config.get(
            "rss_enabled", True
        ):
            diagnostics["fallback_used"] = True
            diagnostics["fallback_reason"] = fallback_reason or "api_empty"
            rss_papers, rss_diagnostics = self.rss_source.fetch_with_diagnostics(
                target_date
            )
            diagnostics["rss"] = rss_diagnostics
            for paper in rss_papers:
                identity = paper_identity(paper)
                if identity in papers:
                    current = papers[identity]
                    current["candidate_topics"] = sorted(
                        set(current["candidate_topics"])
                        | set(paper["candidate_topics"])
                    )
                    current["categories"] = sorted(
                        set(current.get("categories", []))
                        | set(paper.get("categories", []))
                    )
                else:
                    papers[identity] = paper

        result = list(papers.values())
        diagnostics["result_count"] = len(result)
        if result and diagnostics["fallback_used"]:
            diagnostics["status"] = "fallback_rss"
        elif result:
            diagnostics["status"] = "available"
        elif diagnostics["api_topics_failed"]:
            diagnostics["status"] = "failed"
        return result, diagnostics

    def fetch(self, target_date: date) -> list[dict[str, Any]]:
        papers, _ = self.fetch_with_diagnostics(target_date)
        return papers


class SemanticScholarEnricher:
    FIELDS = "citationCount,venue,publicationVenue,year,externalIds,publicationTypes"

    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        self.config = config["sources"]["semantic_scholar"]
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config["sources"]["user_agent"]})
        api_key = secret_from(self.config)
        if api_key:
            self.session.headers.update({"x-api-key": api_key})
        self.interval = 1 / max(
            float(self.config.get("requests_per_second", 0.8)), 0.01
        )
        self._last_request = 0.0

    def _throttle(self) -> None:
        delay = self.interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)

    def enrich(self, paper: dict[str, Any]) -> dict[str, Any]:
        arxiv_id = paper.get("arxiv_id")
        if not arxiv_id:
            paper["semantic_scholar_status"] = "not_applicable"
            return paper
        url = f"{self.config['base_url'].rstrip('/')}/paper/arXiv:{arxiv_id}"
        retries = int(self.config.get("max_retries", 3))
        for attempt in range(retries):
            self._throttle()
            try:
                response = self.session.get(
                    url,
                    params={"fields": self.FIELDS},
                    timeout=float(self.config.get("timeout_seconds", 20)),
                )
                self._last_request = time.monotonic()
                if response.status_code == 404:
                    paper["semantic_scholar_status"] = "not_found"
                    return paper
                if response.status_code == 429:
                    time.sleep(min(2 ** (attempt + 1), 20))
                    continue
                response.raise_for_status()
                payload = response.json()
                publication_venue = payload.get("publicationVenue") or {}
                venue = payload.get("venue") or publication_venue.get("name") or ""
                citation_count = max(
                    int(paper.get("citation_count") or 0),
                    int(payload.get("citationCount") or 0),
                )
                paper.update(
                    {
                        "citation_count": citation_count,
                        "venue": venue or paper.get("venue", ""),
                        "publication_year": payload.get("year"),
                        "publication_types": payload.get("publicationTypes") or [],
                        "semantic_scholar_external_ids": payload.get("externalIds")
                        or {},
                        "semantic_scholar_status": "available",
                    }
                )
                return paper
            except (requests.RequestException, ValueError) as exc:
                LOGGER.warning(
                    "Semantic Scholar failed for %s (%s/%s): %s",
                    arxiv_id,
                    attempt + 1,
                    retries,
                    type(exc).__name__,
                )
                if attempt + 1 < retries:
                    time.sleep(min(2**attempt, 10))
        paper["semantic_scholar_status"] = "request_failed"
        return paper

    def enrich_many(self, papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.enrich(paper) for paper in papers]


class ArxivFigureEnricher:
    def __init__(
        self,
        config: dict[str, Any],
        session: requests.Session | None = None,
        report_date: date | None = None,
    ):
        self.config = config["sources"]["arxiv_html"]
        self.app_project = config["project"]
        self.app_render = config["render"]
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config["sources"]["user_agent"]})
        self.project_root = Path(config["_meta"]["project_root"])
        self.report_date = report_date or date.today()
        requests_per_second = float(self.config.get("requests_per_second", 0.5))
        self.interval = 1 / max(requests_per_second, 0.01)
        self._last_request = 0.0

    def _download_image_bytes(self, response: requests.Response) -> bytes:
        max_bytes = int(self.config.get("max_image_bytes", 10_000_000))
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("image_too_large")
        if hasattr(response, "iter_content"):
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("image_too_large")
                chunks.append(chunk)
            return b"".join(chunks)
        content = response.content
        if len(content) > max_bytes:
            raise ValueError("image_too_large")
        return content

    def _normalize_image(self, content: bytes) -> bytes:
        allowed_formats = {
            str(item).upper()
            for item in self.config.get("allowed_formats", ["PNG", "JPEG", "WEBP"])
        }
        with Image.open(BytesIO(content)) as image:
            if str(image.format).upper() not in allowed_formats:
                raise ValueError("unsupported_image_format")
            max_pixels = int(self.config.get("max_image_pixels", 40_000_000))
            if image.width * image.height > max_pixels:
                raise ValueError("image_pixel_limit_exceeded")
            image.load()
            normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = BytesIO()
            normalized.save(output, format="PNG", optimize=True)
            return output.getvalue()

    def _validate_and_cache_image(
        self, paper: dict[str, Any], image_url: str
    ) -> dict[str, Any]:
        if not self.config.get("validate_images", True) and not self.config.get(
            "cache_images", True
        ):
            paper.update(
                {
                    "figure_url": image_url,
                    "figure_source_url": image_url,
                    "figure_status": "available",
                    "figure_status_detail": "remote_url_unvalidated",
                }
            )
            return paper
        try:
            image_response = self.session.get(
                image_url,
                timeout=float(self.config.get("timeout_seconds", 20)),
                stream=True,
            )
            content_type = image_response.headers.get("content-type", "").lower()
            if image_response.status_code != 200 or not content_type.startswith(
                "image/"
            ):
                paper.update(
                    {
                        "figure_url": None,
                        "figure_source_url": image_url,
                        "figure_status": "image_unavailable",
                        "figure_http_status": image_response.status_code,
                        "figure_status_detail": f"expected image/*, received {content_type or 'unknown'}",
                    }
                )
                return paper
            paper["figure_source_url"] = image_url
            paper["figure_http_status"] = image_response.status_code
            downloaded_image = self._download_image_bytes(image_response)
            normalized_image = self._normalize_image(downloaded_image)
            if not self.config.get("cache_images", True):
                paper.update(
                    {
                        "figure_url": image_url,
                        "figure_status": "available",
                        "figure_status_detail": "remote_image_validated",
                    }
                )
                return paper
            safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(paper.get("arxiv_id")))
            relative_path = (
                Path(self.config.get("cache_dir", "assets/figures"))
                / f"{self.report_date:%Y-%m}"
                / report_slug(
                    self.report_date,
                    {"project": self.app_project, "render": self.app_render},
                )
                / f"{safe_id}-figure1.png"
            )
            cache_path = self.project_root / relative_path
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = cache_path.with_suffix(".tmp")
            temporary_path.write_bytes(normalized_image)
            temporary_path.replace(cache_path)
            paper.update(
                {
                    "figure_url": relative_path.as_posix(),
                    "figure_cache_path": relative_path.as_posix(),
                    "figure_status": "available",
                    "figure_status_detail": "cached_local_copy",
                }
            )
            return paper
        except (
            requests.RequestException,
            OSError,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ) as exc:
            LOGGER.warning("Figure image validation failed: %s", type(exc).__name__)
            paper.update(
                {
                    "figure_url": None,
                    "figure_source_url": image_url,
                    "figure_status": "image_request_failed",
                    "figure_status_detail": "download_or_validation_failed",
                }
            )
            return paper

    def enrich(self, paper: dict[str, Any]) -> dict[str, Any]:
        arxiv_id = paper.get("arxiv_id")
        if not arxiv_id:
            paper["figure_status"] = "not_applicable"
            return paper
        delay = self.interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        html_url = f"{self.config['base_url'].rstrip('/')}/{arxiv_id}"
        try:
            response = self.session.get(
                html_url,
                timeout=float(self.config.get("timeout_seconds", 20)),
            )
            self._last_request = time.monotonic()
            if response.status_code in {404, 406}:
                paper["figure_status"] = "html_unavailable"
                paper["figure_status_detail"] = (
                    f"arXiv HTML returned {response.status_code}"
                )
                return paper
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            figure = soup.find("figure")
            image = figure.find("img") if figure else None
            if not image:
                paper["figure_status"] = "no_figure"
                paper["figure_status_detail"] = "first HTML figure has no img element"
                return paper
            image_src = html_attribute(image, "src")
            srcset = html_attribute(image, "srcset")
            if not image_src and srcset:
                image_src = srcset.split(",")[0].strip().split(" ")[0]
            if not image_src:
                paper["figure_status"] = "no_figure"
                paper["figure_status_detail"] = "first HTML figure has no src or srcset"
                return paper
            caption = figure.find("figcaption") if figure else None
            image_url = urljoin(response.url, image_src)
            paper.update(
                {
                    "figure_caption": caption.get_text(" ", strip=True)
                    if caption
                    else "",
                    "arxiv_html_url": response.url,
                }
            )
            return self._validate_and_cache_image(paper, image_url)
        except requests.RequestException as exc:
            LOGGER.warning("arXiv HTML request failed: %s", type(exc).__name__)
            paper["figure_status"] = "request_failed"
            paper["figure_status_detail"] = "html_request_failed"
        return paper

    def enrich_many(self, papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.enrich(paper) for paper in papers]


class NeurIPSSource:
    """Official NeurIPS proceedings adapter.

    The official proceedings establish venue identity.  Presentation labels are
    accepted only when a title is listed on the official papers, oral, or
    spotlight event pages.  Paper-title words are never used as labels.
    """

    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        self.app_config = config
        self.config = config["sources"]["neurips"]
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config["sources"]["user_agent"]})

    def _candidate_years(self, target_date: date) -> list[int]:
        # Proceedings generally become available near the conference in December.
        return [target_date.year, target_date.year - 1]

    @staticmethod
    def _valid_paper_title(anchor: Any) -> str:
        title = anchor.get_text(" ", strip=True)
        if not title or title.lower() in {"view full details", "details"}:
            return ""
        return title if len(title) >= 8 else ""

    def _presentation_map(self, year: int) -> dict[str, str]:
        url = f"{self.config['virtual_base_url'].rstrip('/')}/{year}/papers.html"
        mapping: dict[str, str] = {}
        try:
            response = self.session.get(
                url, timeout=self.config.get("timeout_seconds", 25)
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                if f"/virtual/{year}/poster/" not in html_attribute(anchor, "href"):
                    continue
                title = self._valid_paper_title(anchor)
                if title:
                    mapping[normalize_title(title)] = "Poster"

            location_base = response.url.rsplit("/papers.html", 1)[0]
            event_urls: dict[str, str] = {}
            for anchor in soup.find_all("a", href=True):
                event_url = urljoin(response.url, html_attribute(anchor, "href"))
                lower_url = event_url.lower().rstrip("/")
                if location_base not in event_url:
                    continue
                if lower_url.endswith("/events/oral"):
                    event_urls["Oral"] = event_url
                elif "/events/spotlight" in lower_url:
                    event_urls["Spotlight"] = event_url

            # Apply Spotlight first, then Oral, so an official oral listing wins.
            for presentation in ("Spotlight", "Oral"):
                event_url = event_urls.get(presentation)
                if not event_url:
                    continue
                try:
                    event_response = self.session.get(
                        event_url, timeout=self.config.get("timeout_seconds", 25)
                    )
                    event_response.raise_for_status()
                except requests.RequestException as exc:
                    LOGGER.info(
                        "NeurIPS %s page unavailable for %s: %s",
                        presentation,
                        year,
                        type(exc).__name__,
                    )
                    continue
                event_soup = BeautifulSoup(event_response.text, "html.parser")
                path_token = "oral" if presentation == "Oral" else "poster"
                expected_path = f"/virtual/{year}/{path_token}/"
                for anchor in event_soup.find_all("a", href=True):
                    if expected_path not in html_attribute(anchor, "href"):
                        continue
                    title = self._valid_paper_title(anchor)
                    if title:
                        mapping[normalize_title(title)] = presentation
        except requests.RequestException as exc:
            LOGGER.info(
                "NeurIPS virtual page unavailable for %s: %s",
                year,
                type(exc).__name__,
            )
        return mapping

    def fetch(self, target_date: date) -> list[dict[str, Any]]:
        event_date = date(target_date.year, 12, 1)
        window = int(self.config.get("candidate_window_days", 45))
        include_as_candidates = abs((target_date - event_date).days) <= window
        if not include_as_candidates:
            LOGGER.info(
                "Skipping NeurIPS proceedings candidates outside the configured conference window"
            )
            return []
        papers: list[dict[str, Any]] = []
        for year in [target_date.year]:
            index_url = f"{self.config['proceedings_base_url'].rstrip('/')}/paper_files/paper/{year}"
            try:
                response = self.session.get(
                    index_url, timeout=self.config.get("timeout_seconds", 25)
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
            except requests.RequestException as exc:
                LOGGER.info(
                    "NeurIPS proceedings unavailable for %s: %s",
                    year,
                    type(exc).__name__,
                )
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            presentation_map = self._presentation_map(year)
            matched_links: list[tuple[str, str, list[str]]] = []
            for anchor in soup.find_all("a", href=True):
                href = html_attribute(anchor, "href")
                if "-Abstract-Conference.html" not in href:
                    continue
                title = anchor.get_text(" ", strip=True)
                topic_ids = _contains_topic_keyword(title, self.app_config["topics"])
                if topic_ids:
                    matched_links.append(
                        (title, urljoin(response.url, href), topic_ids)
                    )
            detail_limit = int(self.config.get("max_detail_pages", 80))
            for title, detail_url, topic_ids in matched_links[:detail_limit]:
                try:
                    detail_response = self.session.get(
                        detail_url, timeout=self.config.get("timeout_seconds", 25)
                    )
                    detail_response.raise_for_status()
                    detail_soup = BeautifulSoup(detail_response.text, "html.parser")
                except requests.RequestException as exc:
                    LOGGER.info(
                        "NeurIPS paper detail unavailable: %s", type(exc).__name__
                    )
                    continue
                text_sections: dict[str, str] = {}
                for heading in detail_soup.find_all(["h3", "h4"]):
                    sibling = heading.find_next_sibling()
                    if sibling:
                        text_sections[heading.get_text(" ", strip=True).lower()] = (
                            sibling.get_text(" ", strip=True)
                        )
                pdf_anchor = detail_soup.find(
                    "a", href=re.compile(r"\.pdf$", re.IGNORECASE)
                )
                paper = {
                    "paper_id": f"neurips:{year}:{normalize_title(title)}",
                    "source": "neurips_official",
                    "sources": ["neurips_official"],
                    "source_id": detail_url,
                    "arxiv_id": None,
                    "title": title,
                    "summary": text_sections.get("abstract", ""),
                    "url": detail_url,
                    "abstract_url": detail_url,
                    "pdf_url": urljoin(
                        detail_response.url, html_attribute(pdf_anchor, "href")
                    )
                    if pdf_anchor
                    else None,
                    "published_date": f"{year}-12-01T00:00:00+00:00",
                    "updated_date": None,
                    "categories": [],
                    "authors": [
                        name.strip()
                        for name in re.split(
                            r",| and ", text_sections.get("authors", "")
                        )
                        if name.strip()
                    ],
                    "candidate_topics": topic_ids,
                    "citation_count": 0,
                    "venue": f"NeurIPS {year}",
                    "venue_tags": ["NeurIPS"],
                    "presentation_type": presentation_map.get(
                        normalize_title(title), ""
                    ),
                    "official_venue_verified": True,
                    "figure_url": None,
                    "figure_caption": "",
                    "figure_status": "not_applicable",
                    "eligible_by_date": include_as_candidates
                    and year == target_date.year,
                }
                papers.append(paper)
            if matched_links:
                break
        return papers


def _abstract_from_inverted_index(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in value.items():
        if not isinstance(positions, list):
            continue
        positioned.extend((int(position), str(word)) for position in positions)
    return " ".join(word for _, word in sorted(positioned))


def _configured_venue_tag(venue: str, config: dict[str, Any]) -> str | None:
    normalized = f" {normalize_title(venue)} "
    disqualifying_markers = (
        " workshop ",
        " workshops ",
        " tutorial ",
        " challenge ",
        " companion ",
        " demo track ",
        " doctoral consortium ",
        " co located ",
        " co-located ",
    )
    if any(marker in normalized for marker in disqualifying_markers):
        return None
    aliases = config["selection"].get("venue_aliases", {})
    for tag in config["selection"].get("top_venues", []):
        for alias in aliases.get(tag, [tag]):
            normalized_alias = normalize_title(str(alias))
            if normalized_alias and f" {normalized_alias} " in normalized:
                return str(tag)
    return None


class OpenAlexSource:
    """Config-driven adapter for recent papers from ML/AI top venues."""

    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        self.app_config = config
        self.config = config["sources"].get("openalex", {})
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config["sources"]["user_agent"]})
        self.interval = 1 / max(
            float(self.config.get("requests_per_second", 1.0)), 0.01
        )
        self._last_request = 0.0

    def _throttle(self) -> None:
        delay = self.interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _arxiv_id(work: dict[str, Any]) -> str | None:
        ids = work.get("ids") or {}
        for key, value in ids.items():
            if "arxiv" in str(key).lower() or "arxiv.org" in str(value).lower():
                found = extract_arxiv_id(str(value))
                if found:
                    return found
        for location_key in ("primary_location", "best_oa_location"):
            location = work.get(location_key) or {}
            for field in ("landing_page_url", "pdf_url"):
                value = location.get(field)
                if value and "arxiv.org" in str(value).lower():
                    found = extract_arxiv_id(str(value))
                    if found:
                        return found
        for location in work.get("locations") or []:
            for field in ("landing_page_url", "pdf_url"):
                value = location.get(field)
                if value and "arxiv.org" in str(value).lower():
                    found = extract_arxiv_id(str(value))
                    if found:
                        return found
        return None

    def _paper_from_work(
        self,
        work: dict[str, Any],
        topic_id: str,
        *,
        require_top_venue: bool = True,
        source_name: str = "openalex",
    ) -> dict[str, Any] | None:
        primary_location = work.get("primary_location") or {}
        locations = [primary_location, *(work.get("locations") or [])]
        venue = ""
        fallback_venue = ""
        venue_tag = None
        for location in locations:
            source = location.get("source") or {}
            candidate_venue = str(
                source.get("display_name") or location.get("raw_source_name") or ""
            )
            if candidate_venue and not fallback_venue:
                fallback_venue = candidate_venue
            candidate_tag = _configured_venue_tag(candidate_venue, self.app_config)
            if candidate_tag:
                venue = candidate_venue
                venue_tag = candidate_tag
                break
        if require_top_venue and not venue_tag:
            return None
        venue = venue or fallback_venue
        best_oa = work.get("best_oa_location") or {}
        ids = work.get("ids") or {}
        arxiv_id = self._arxiv_id(work)
        url = (
            primary_location.get("landing_page_url")
            or ids.get("doi")
            or work.get("doi")
            or work.get("id")
        )
        title = str(work.get("title") or work.get("display_name") or "").strip()
        if not title:
            return None
        authors = [
            str((authorship.get("author") or {}).get("display_name"))
            for authorship in work.get("authorships") or []
            if (authorship.get("author") or {}).get("display_name")
        ]
        publication_date = str(work.get("publication_date") or "")
        return {
            "paper_id": f"openalex:{str(work.get('id') or normalize_title(title)).rsplit('/', 1)[-1]}",
            "source": source_name,
            "sources": [source_name],
            "source_id": work.get("id"),
            "arxiv_id": arxiv_id,
            "title": title,
            "summary": _abstract_from_inverted_index(
                work.get("abstract_inverted_index")
            ),
            "url": url,
            "abstract_url": url,
            "pdf_url": best_oa.get("pdf_url") or primary_location.get("pdf_url"),
            "published_date": f"{publication_date}T00:00:00+00:00"
            if publication_date
            else None,
            "updated_date": None,
            "categories": [],
            "authors": authors,
            "candidate_topics": [topic_id],
            "citation_count": int(work.get("cited_by_count") or 0),
            "venue": venue,
            "venue_tags": [venue_tag] if venue_tag else [],
            "presentation_type": "",
            "official_venue_verified": False,
            "figure_url": None,
            "figure_caption": "",
            "figure_status": "not_requested" if arxiv_id else "not_applicable",
        }

    def fetch(self, target_date: date) -> list[dict[str, Any]]:
        lookback = int(self.config.get("lookback_days", 3))
        start_date = target_date - timedelta(days=max(lookback - 1, 0))
        endpoint = f"{self.config.get('base_url', 'https://api.openalex.org').rstrip('/')}/works"
        retries = int(self.config.get("max_retries", 3))
        papers: list[dict[str, Any]] = []
        max_topics = max(int(self.config.get("max_topics_per_run", 4)), 1)
        ordered_topics = list(self.app_config["topics"].items())[:max_topics]
        for topic_id, topic in ordered_topics:
            params: dict[str, Any] = {
                "search": " OR ".join(str(item) for item in topic["keywords"][:10]),
                "filter": (
                    f"from_publication_date:{start_date.isoformat()},"
                    f"to_publication_date:{target_date.isoformat()}"
                ),
                "sort": "publication_date:desc",
                "per-page": min(int(self.config.get("max_results_per_topic", 25)), 100),
            }
            mailto_env = self.config.get("mailto_env")
            if mailto_env and os.getenv(str(mailto_env)):
                params["mailto"] = os.getenv(str(mailto_env))
            for attempt in range(retries):
                self._throttle()
                try:
                    response = self.session.get(
                        endpoint,
                        params=params,
                        timeout=float(self.config.get("timeout_seconds", 25)),
                    )
                    self._last_request = time.monotonic()
                    if response.status_code == 429:
                        time.sleep(min(2 ** (attempt + 1), 20))
                        continue
                    response.raise_for_status()
                    for work in response.json().get("results", []):
                        paper = self._paper_from_work(work, topic_id)
                        if paper:
                            papers.append(paper)
                    break
                except (requests.RequestException, ValueError, TypeError) as exc:
                    LOGGER.warning(
                        "OpenAlex topic %s attempt %s/%s failed: %s",
                        topic_id,
                        attempt + 1,
                        retries,
                        type(exc).__name__,
                    )
                    if attempt + 1 < retries:
                        time.sleep(min(2**attempt, 10))
        return papers


class HistoricalOpenAlexSource(OpenAlexSource):
    """Discover older, high-impact papers for the scheduled focus topic."""

    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        super().__init__(config, session=session)
        self.config = {
            **config["sources"].get("openalex", {}),
            **config["sources"].get("openalex_historical", {}),
        }
        self.interval = 1 / max(
            float(self.config.get("requests_per_second", 1.0)), 0.01
        )

    def _request_pages(
        self, endpoint: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        retries = int(self.config.get("max_retries", 3))
        max_pages = max(int(self.config.get("max_pages", 2)), 1)
        cursor = "*"
        works: list[dict[str, Any]] = []
        for _ in range(max_pages):
            page_params = {**params, "cursor": cursor}
            for attempt in range(retries):
                self._throttle()
                try:
                    response = self.session.get(
                        endpoint,
                        params=page_params,
                        timeout=float(self.config.get("timeout_seconds", 25)),
                    )
                    self._last_request = time.monotonic()
                    if response.status_code == 429:
                        time.sleep(min(2 ** (attempt + 1), 20))
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    works.extend(payload.get("results", []))
                    cursor = str((payload.get("meta") or {}).get("next_cursor") or "")
                    break
                except (requests.RequestException, ValueError, TypeError) as exc:
                    LOGGER.warning(
                        "Historical OpenAlex page attempt %s/%s failed: %s",
                        attempt + 1,
                        retries,
                        type(exc).__name__,
                    )
                    if attempt + 1 < retries:
                        time.sleep(min(2**attempt, 10))
            else:
                break
            if not cursor:
                break
        return works

    def fetch(self, target_date: date, topic_id: str) -> list[dict[str, Any]]:
        topic = self.app_config["topics"][topic_id]
        recent_days = int(self.app_config["selection"].get("recent_days", 120))
        cutoff_date = target_date - timedelta(days=recent_days + 1)
        threshold = int(
            self.app_config["selection"].get("high_citation_threshold", 100)
        )
        endpoint = f"{self.config.get('base_url', 'https://api.openalex.org').rstrip('/')}/works"
        base_params: dict[str, Any] = {
            "search": " OR ".join(str(item) for item in topic["keywords"][:10]),
            "per-page": min(int(self.config.get("max_results", 50)), 100),
        }
        mailto_env = self.config.get("mailto_env")
        if mailto_env and os.getenv(str(mailto_env)):
            base_params["mailto"] = os.getenv(str(mailto_env))
        cited_params = {
            **base_params,
            "filter": (
                f"to_publication_date:{cutoff_date.isoformat()},"
                f"cited_by_count:>{max(threshold - 1, 0)}"
            ),
            "sort": "cited_by_count:desc",
        }
        venue_params = {
            **base_params,
            "filter": (
                f"to_publication_date:{cutoff_date.isoformat()},"
                "primary_location.source.display_name.search:"
                + "|".join(
                    normalize_title(str(alias))
                    for tag in self.app_config["selection"].get("well_known_venues", [])
                    for alias in self.app_config["selection"]
                    .get("venue_aliases", {})
                    .get(tag, [tag])
                    if normalize_title(str(alias))
                )
            ),
            "sort": "publication_date:desc",
        }
        works: dict[str, dict[str, Any]] = {}
        for work in [
            *self._request_pages(endpoint, cited_params),
            *self._request_pages(endpoint, venue_params),
        ]:
            identity = str(work.get("id") or normalize_title(work.get("title", "")))
            works[identity] = work
        papers: list[dict[str, Any]] = []
        for work in works.values():
            paper = self._paper_from_work(
                work,
                topic_id,
                require_top_venue=False,
                source_name="openalex_historical",
            )
            if not paper:
                continue
            published = str(paper.get("published_date") or "")[:10]
            if published and published > cutoff_date.isoformat():
                continue
            if int(paper.get("citation_count") or 0) < threshold and not (
                set(paper.get("venue_tags", []))
                & set(self.app_config["selection"].get("well_known_venues", []))
            ):
                continue
            paper["historical_discovery"] = True
            papers.append(paper)
        return papers


def merge_papers(papers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    by_title: dict[str, str] = {}
    for paper in papers:
        identity = paper_identity(paper)
        normalized_title = normalize_title(paper.get("title", ""))
        existing_key = (
            identity if identity in merged else by_title.get(normalized_title)
        )
        if not existing_key:
            merged[identity] = paper
            by_title[normalized_title] = identity
            continue
        current = merged[existing_key]
        current["sources"] = sorted(
            set(current.get("sources", [])) | set(paper.get("sources", []))
        )
        current["candidate_topics"] = sorted(
            set(current.get("candidate_topics", []))
            | set(paper.get("candidate_topics", []))
        )
        for field in (
            "arxiv_id",
            "venue",
            "presentation_type",
            "official_venue_verified",
            "pdf_url",
            "summary",
        ):
            if paper.get(field) and (
                not current.get(field) or paper.get("official_venue_verified")
            ):
                current[field] = paper[field]
        current["venue_tags"] = sorted(
            set(current.get("venue_tags", [])) | set(paper.get("venue_tags", []))
        )
        current["citation_count"] = max(
            int(current.get("citation_count") or 0),
            int(paper.get("citation_count") or 0),
        )
        current["historical_discovery"] = bool(
            current.get("historical_discovery") or paper.get("historical_discovery")
        )
    return list(merged.values())


def derive_venue_tags(paper: dict[str, Any], config: dict[str, Any]) -> list[str]:
    configured = {str(tag) for tag in config["selection"].get("top_venues", [])}
    tags = {str(tag) for tag in paper.get("venue_tags", []) if str(tag) in configured}
    if venue_tag := _configured_venue_tag(str(paper.get("venue") or ""), config):
        tags.add(venue_tag)
    # Prefer the modern name when both strings occur.
    if "NeurIPS" in tags and "NIPS" in tags:
        tags.remove("NIPS")
    return sorted(tags)


def crawl_papers_with_diagnostics(
    target_date: date,
    config: dict[str, Any] | None = None,
    *,
    enrich_semantic_scholar: bool = True,
    topic_order: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch configured sources and return papers plus public-safe diagnostics.

    Figure extraction is intentionally deferred until after selection via
    :func:`enrich_selected_figures`, avoiding hundreds of arXiv HTML requests.
    """

    config = config or load_config()
    if topic_order:
        ordered_ids = [
            topic_id for topic_id in topic_order if topic_id in config["topics"]
        ]
        ordered_ids.extend(
            topic_id for topic_id in config["topics"] if topic_id not in ordered_ids
        )
        config = deepcopy(config)
        config["topics"] = {
            topic_id: config["topics"][topic_id] for topic_id in ordered_ids
        }
    source_papers: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    if config["sources"]["arxiv"].get("enabled", True):
        arxiv_papers, arxiv_diagnostics = ArxivSource(config).fetch_with_diagnostics(
            target_date
        )
        source_papers.extend(arxiv_papers)
        diagnostics["arxiv"] = arxiv_diagnostics
    else:
        diagnostics["arxiv"] = {"status": "disabled", "result_count": 0}
    if config["sources"]["neurips"].get("enabled", True):
        neurips_papers = NeurIPSSource(config).fetch(target_date)
        source_papers.extend(neurips_papers)
        diagnostics["neurips"] = {
            "status": "available" if neurips_papers else "empty",
            "result_count": len(neurips_papers),
        }
    else:
        diagnostics["neurips"] = {"status": "disabled", "result_count": 0}
    if config["sources"].get("openalex", {}).get("enabled", True):
        openalex_papers = OpenAlexSource(config).fetch(target_date)
        source_papers.extend(openalex_papers)
        diagnostics["openalex"] = {
            "status": "available" if openalex_papers else "empty",
            "result_count": len(openalex_papers),
        }
    else:
        diagnostics["openalex"] = {"status": "disabled", "result_count": 0}
    historical_config = config["sources"].get("openalex_historical", {})
    if historical_config.get("enabled", True):
        focus_topic = next(iter(config["topics"]))
        try:
            historical_papers = HistoricalOpenAlexSource(config).fetch(
                target_date, focus_topic
            )
            historical_status = "available" if historical_papers else "empty"
        except Exception as exc:  # preserve independent-source failure semantics
            LOGGER.warning("Historical OpenAlex source failed: %s", type(exc).__name__)
            historical_papers = []
            historical_status = "failed"
        source_papers.extend(historical_papers)
        diagnostics["openalex_historical"] = {
            "status": historical_status,
            "result_count": len(historical_papers),
            "focus_topic": focus_topic,
        }
    else:
        diagnostics["openalex_historical"] = {
            "status": "disabled",
            "result_count": 0,
        }
    papers = merge_papers(source_papers)
    diagnostics["merged_candidate_count"] = len(papers)

    if enrich_semantic_scholar and config["sources"]["semantic_scholar"].get(
        "enabled", True
    ):
        papers = SemanticScholarEnricher(config).enrich_many(papers)
        status_counts: dict[str, int] = {}
        for paper in papers:
            status = str(paper.get("semantic_scholar_status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        diagnostics["semantic_scholar"] = {
            "status": "completed",
            "paper_count": len(papers),
            "status_counts": status_counts,
        }
    else:
        diagnostics["semantic_scholar"] = {
            "status": "disabled" if not enrich_semantic_scholar else "skipped",
            "paper_count": len(papers),
        }
    for paper in papers:
        paper["venue_tags"] = sorted(
            set(paper.get("venue_tags", [])) | set(derive_venue_tags(paper, config))
        )
        annotate_well_known_paper(paper, target_date, config)
        paper["metadata_quality_score"] = round(
            math.log1p(max(int(paper.get("citation_count", 0)), 0)), 3
        )
    return papers, diagnostics


def crawl_papers(
    target_date: date,
    config: dict[str, Any] | None = None,
    *,
    enrich_semantic_scholar: bool = True,
    topic_order: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper returning only merged papers."""

    papers, _ = crawl_papers_with_diagnostics(
        target_date,
        config,
        enrich_semantic_scholar=enrich_semantic_scholar,
        topic_order=topic_order,
    )
    return papers


def enrich_selected_figures(
    papers: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    report_date: date | None = None,
) -> list[dict[str, Any]]:
    config = config or load_config()
    for paper in papers:
        if paper.get("content_tier") != "major":
            paper["figure_url"] = None
            paper["figure_status"] = "not_requested"
    if not config["sources"]["arxiv_html"].get("enabled", True):
        return papers
    enricher = ArxivFigureEnricher(config, report_date=report_date)
    for paper in papers:
        if paper.get("content_tier") == "major":
            enricher.enrich(paper)
    return papers


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    items = crawl_papers(date.today())
    print(f"Fetched {len(items)} merged papers")
