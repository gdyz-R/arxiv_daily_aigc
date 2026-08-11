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
from datetime import date, datetime, timedelta, timezone
from datetime import time as datetime_time
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import arxiv
import requests
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

try:
    from archive import report_slug
    from config import load_config, secret_from
except ImportError:  # pragma: no cover - package execution
    from .archive import report_slug
    from .config import load_config, secret_from


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


def _keyword_clause(keywords: Iterable[str]) -> str:
    clauses: list[str] = []
    for keyword in keywords:
        escaped = keyword.replace('"', r"\"")
        clauses.extend((f'ti:"{escaped}"', f'abs:"{escaped}"'))
    return " OR ".join(clauses)


def build_arxiv_query(
    topic: dict[str, Any], target_date: date, lookback_days: int
) -> str:
    categories = " OR ".join(
        f"cat:{category}" for category in topic.get("categories", [])
    )
    keywords = _keyword_clause(topic["keywords"])
    start = datetime.combine(
        target_date - timedelta(days=max(lookback_days - 1, 0)),
        datetime_time.min,
        tzinfo=timezone.utc,
    )
    end = datetime.combine(
        target_date + timedelta(days=1), datetime_time.min, tzinfo=timezone.utc
    )
    date_range = f"submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]"
    category_clause = f"({categories}) AND " if categories else ""
    return f"{category_clause}({keywords}) AND {date_range}"


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


class ArxivSource:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        source_config = config["sources"]["arxiv"]
        self.source_config = source_config
        self.client = arxiv.Client(
            delay_seconds=float(source_config.get("delay_seconds", 3)),
            num_retries=int(source_config.get("num_retries", 4)),
        )

    def fetch(self, target_date: date) -> list[dict[str, Any]]:
        papers: dict[str, dict[str, Any]] = {}
        limit = min(
            int(self.source_config.get("max_results_per_topic", 45)),
            int(self.config["selection"].get("candidate_limit_per_topic", 45)),
        )
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
            try:
                for result in self.client.results(search):
                    arxiv_id = extract_arxiv_id(result.entry_id)
                    paper = {
                        "paper_id": f"arxiv:{arxiv_id}"
                        if arxiv_id
                        else result.entry_id,
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
                    identity = paper_identity(paper)
                    if identity in papers:
                        current = papers[identity]
                        current["candidate_topics"] = sorted(
                            set(current["candidate_topics"]) | {topic_id}
                        )
                    else:
                        papers[identity] = paper
            except Exception as exc:  # arxiv library exposes several transport errors
                LOGGER.warning(
                    "arXiv query failed for %s: %s", topic_id, type(exc).__name__
                )
        return list(papers.values())


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
                paper.update(
                    {
                        "citation_count": int(payload.get("citationCount") or 0),
                        "venue": venue,
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
        self, work: dict[str, Any], topic_id: str
    ) -> dict[str, Any] | None:
        primary_location = work.get("primary_location") or {}
        locations = [primary_location, *(work.get("locations") or [])]
        venue = ""
        venue_tag = None
        for location in locations:
            source = location.get("source") or {}
            candidate_venue = str(
                source.get("display_name") or location.get("raw_source_name") or ""
            )
            candidate_tag = _configured_venue_tag(candidate_venue, self.app_config)
            if candidate_tag:
                venue = candidate_venue
                venue_tag = candidate_tag
                break
        if not venue_tag:
            return None
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
            "source": "openalex",
            "sources": ["openalex"],
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
            "venue_tags": [venue_tag],
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
        for topic_id, topic in self.app_config["topics"].items():
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
    return list(merged.values())


def derive_venue_tags(paper: dict[str, Any], config: dict[str, Any]) -> list[str]:
    venue_text = (
        f"{paper.get('venue', '')} {' '.join(paper.get('venue_tags', []))}".lower()
    )
    aliases = config["selection"].get("venue_aliases", {})
    tags = [
        str(tag)
        for tag in config["selection"].get("top_venues", [])
        if any(
            f" {normalize_title(str(alias))} " in f" {normalize_title(venue_text)} "
            for alias in aliases.get(tag, [tag])
        )
    ]
    # Prefer the modern name when both strings occur.
    if "NeurIPS" in tags and "NIPS" in tags:
        tags.remove("NIPS")
    return tags


def crawl_papers(
    target_date: date,
    config: dict[str, Any] | None = None,
    *,
    enrich_semantic_scholar: bool = True,
) -> list[dict[str, Any]]:
    """Fetch and merge configured paper sources.

    Figure extraction is intentionally deferred until after selection via
    :func:`enrich_selected_figures`, avoiding hundreds of arXiv HTML requests.
    """

    config = config or load_config()
    source_papers: list[dict[str, Any]] = []
    if config["sources"]["arxiv"].get("enabled", True):
        source_papers.extend(ArxivSource(config).fetch(target_date))
    if config["sources"]["neurips"].get("enabled", True):
        source_papers.extend(NeurIPSSource(config).fetch(target_date))
    if config["sources"].get("openalex", {}).get("enabled", True):
        source_papers.extend(OpenAlexSource(config).fetch(target_date))
    papers = merge_papers(source_papers)

    if enrich_semantic_scholar and config["sources"]["semantic_scholar"].get(
        "enabled", True
    ):
        papers = SemanticScholarEnricher(config).enrich_many(papers)
    for paper in papers:
        paper["venue_tags"] = sorted(
            set(paper.get("venue_tags", [])) | set(derive_venue_tags(paper, config))
        )
        paper["metadata_quality_score"] = round(
            math.log1p(max(int(paper.get("citation_count", 0)), 0)), 3
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
