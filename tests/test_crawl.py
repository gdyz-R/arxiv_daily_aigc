from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import cast

import requests
from PIL import Image

from src.config import load_config
from src.crawl import (
    ArxivFigureEnricher,
    ArxivRSSSource,
    ArxivSource,
    HistoricalOpenAlexSource,
    NeurIPSSource,
    OpenAlexSource,
    SemanticScholarEnricher,
    build_arxiv_query,
    derive_venue_tags,
    enrich_selected_figures,
    extract_arxiv_id,
)


class FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        url="https://example.test",
        text="",
        payload=None,
        headers=None,
        content=b"",
    ):
        self.status_code = status_code
        self.url = url
        self.text = text
        self._payload = payload or {}
        self.headers = headers or {}
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.last_url = None
        self.calls = 0

    def get(self, url, **kwargs):
        self.last_url = url
        self.calls += 1
        return self.response


class RoutingSession:
    def __init__(self, responses):
        self.responses = responses
        self.headers = {}

    def get(self, url, **kwargs):
        return self.responses[url]


class FakeArxivHTTPError(RuntimeError):
    status = 429


class RateLimitedArxivClient:
    def __init__(self):
        self.calls = 0

    def results(self, search):
        del search
        self.calls += 1
        raise FakeArxivHTTPError("rate limited")


class FakeRSSSource:
    def __init__(self, papers):
        self.papers = papers
        self.calls = 0

    def fetch_with_diagnostics(self, target_date):
        del target_date
        self.calls += 1
        return self.papers, {
            "status": "available",
            "result_count": len(self.papers),
        }


class CrawlTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_extract_arxiv_id_removes_version(self):
        self.assertEqual(
            extract_arxiv_id("https://arxiv.org/abs/2608.01234v2"), "2608.01234"
        )

    def test_arxiv_query_has_categories_keywords_and_date(self):
        query = build_arxiv_query(
            self.config["topics"]["engineering_deployment"], date(2026, 8, 10), 3
        )
        self.assertIn("cat:cs.DC", query)
        self.assertIn('ti:"KV cache"', query)
        self.assertIn("submittedDate:[202608080000 TO 202608110000]", query)

    def test_arxiv_rss_parses_matching_recent_paper(self):
        rss = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss xmlns:arxiv="http://arxiv.org/schemas/atom"
             xmlns:dc="http://purl.org/dc/elements/1.1/">
          <channel>
            <item>
              <title>Efficient Agent Memory with Tool Use</title>
              <link>https://arxiv.org/abs/2608.01234</link>
              <description>arXiv:2608.01234v1 Announce Type: new Abstract: We propose an AI agent memory system for tool calling.</description>
              <guid isPermaLink="false">oai:arXiv.org:2608.01234v1</guid>
              <category>cs.AI</category>
              <pubDate>Mon, 10 Aug 2026 00:00:00 -0400</pubDate>
              <arxiv:announce_type>new</arxiv:announce_type>
              <dc:creator>Ada Lovelace, Alan Turing</dc:creator>
            </item>
          </channel>
        </rss>"""
        config = deepcopy(self.config)
        config["topics"] = {"cot_agentic_ai": config["topics"]["cot_agentic_ai"]}
        session = FakeSession(FakeResponse(content=rss))
        source = ArxivRSSSource(config, session=cast(requests.Session, session))
        papers, diagnostics = source.fetch_with_diagnostics(date(2026, 8, 11))
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["arxiv_id"], "2608.01234")
        self.assertEqual(papers[0]["candidate_topics"], ["cot_agentic_ai"])
        self.assertEqual(papers[0]["authors"], ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(diagnostics["status"], "available")

    def test_arxiv_429_trips_rss_fallback_once(self):
        fallback_paper = {
            "paper_id": "arxiv:2608.01234",
            "source": "arxiv_rss",
            "sources": ["arxiv_rss"],
            "arxiv_id": "2608.01234",
            "title": "Efficient Agent Memory",
            "summary": "AI agent memory and tool use.",
            "url": "https://arxiv.org/abs/2608.01234",
            "categories": ["cs.AI"],
            "candidate_topics": ["cot_agentic_ai"],
            "citation_count": 0,
            "venue_tags": [],
        }
        client = RateLimitedArxivClient()
        rss_source = FakeRSSSource([fallback_paper])
        source = ArxivSource(
            self.config,
            client=client,
            rss_source=cast(ArxivRSSSource, rss_source),
        )
        papers, diagnostics = source.fetch_with_diagnostics(date(2026, 8, 11))
        self.assertEqual(client.calls, 1)
        self.assertEqual(rss_source.calls, 1)
        self.assertEqual(len(papers), 1)
        self.assertEqual(diagnostics["status"], "fallback_rss")
        self.assertEqual(diagnostics["fallback_reason"], "http_429")
        self.assertEqual(diagnostics["errors"][0]["http_status"], 429)

    def test_semantic_scholar_enrichment(self):
        session = FakeSession(
            FakeResponse(
                payload={
                    "citationCount": 42,
                    "venue": "International Conference on Learning Representations",
                    "year": 2026,
                    "externalIds": {"ArXiv": "2608.01234"},
                }
            )
        )
        enricher = SemanticScholarEnricher(
            self.config, session=cast(requests.Session, session)
        )
        enricher.interval = 0
        paper = enricher.enrich({"arxiv_id": "2608.01234"})
        self.assertEqual(paper["citation_count"], 42)
        self.assertEqual(paper["semantic_scholar_status"], "available")

    def test_figure_extraction_preserves_arxiv_html_directory(self):
        html_url = "https://arxiv.org/html/2608.01234"
        image_url = "https://arxiv.org/html/2608.01234v1/x1.png"
        html = (
            '<figure><img src="2608.01234v1/x1.png">'
            "<figcaption>System overview</figcaption></figure>"
        )
        image_bytes = BytesIO()
        Image.new("RGB", (8, 6), "white").save(image_bytes, format="PNG")
        with tempfile.TemporaryDirectory() as directory:
            config = deepcopy(self.config)
            config["_meta"]["project_root"] = directory
            session = RoutingSession(
                {
                    html_url: FakeResponse(url=html_url, text=html),
                    image_url: FakeResponse(
                        url=image_url,
                        headers={"content-type": "image/png"},
                        content=image_bytes.getvalue(),
                    ),
                }
            )
            enricher = ArxivFigureEnricher(
                config,
                session=cast(requests.Session, session),
                report_date=date(2026, 8, 10),
            )
            enricher.interval = 0
            paper = enricher.enrich({"arxiv_id": "2608.01234"})
            cached = Path(directory) / paper["figure_cache_path"]
            self.assertTrue(cached.exists())
        self.assertEqual(paper["figure_source_url"], image_url)
        self.assertEqual(
            paper["figure_url"],
            "assets/figures/2026-08/2026-08-10-AI研究日报/2608.01234-figure1.png",
        )
        self.assertEqual(paper["figure_status"], "available")
        self.assertEqual(paper["figure_status_detail"], "cached_local_copy")

    def test_invalid_figure_content_is_rejected(self):
        html_url = "https://arxiv.org/html/2608.09999"
        image_url = "https://arxiv.org/html/2608.09999v1/x1.png"
        session = RoutingSession(
            {
                html_url: FakeResponse(
                    url=html_url,
                    text='<figure><img src="2608.09999v1/x1.png"></figure>',
                ),
                image_url: FakeResponse(
                    status_code=404,
                    url=image_url,
                    headers={"content-type": "text/html"},
                    content=b"not an image",
                ),
            }
        )
        enricher = ArxivFigureEnricher(
            self.config, session=cast(requests.Session, session)
        )
        enricher.interval = 0
        paper = enricher.enrich({"arxiv_id": "2608.09999"})
        self.assertEqual(paper["figure_status"], "image_unavailable")
        self.assertIsNone(paper["figure_url"])

    def test_venue_tag_does_not_treat_generic_machine_learning_as_icml(self):
        self.assertEqual(
            derive_venue_tags({"venue": "Journal of Machine Learning"}, self.config), []
        )
        self.assertEqual(
            derive_venue_tags(
                {"venue": "International Conference on Machine Learning"}, self.config
            ),
            ["ICML"],
        )
        self.assertEqual(
            derive_venue_tags(
                {"venue": "ICML Workshop on Efficient Inference"}, self.config
            ),
            [],
        )

    def test_neurips_presentation_uses_official_event_pages(self):
        papers_url = "https://neurips.cc/virtual/2025/papers.html"
        location_url = "https://neurips.cc/virtual/2025/loc/san-diego/papers.html"
        oral_url = "https://neurips.cc/virtual/2025/loc/san-diego/events/oral"
        spotlight_url = (
            "https://neurips.cc/virtual/2025/loc/san-diego/events/spotlights-2025"
        )
        papers_html = """
        <a href="/virtual/2025/loc/san-diego/events/oral">Orals</a>
        <a href="/virtual/2025/loc/san-diego/events/spotlights-2025">Spotlights</a>
        <a href="/virtual/2025/poster/1">Spotlight Attention for KV Cache Retrieval</a>
        <a href="/virtual/2025/poster/2">Official Spotlight Paper</a>
        <a href="/virtual/2025/poster/3">Official Oral Paper</a>
        """
        session = RoutingSession(
            {
                papers_url: FakeResponse(url=location_url, text=papers_html),
                oral_url: FakeResponse(
                    url=oral_url,
                    text='<a href="/virtual/2025/oral/3">Official Oral Paper</a>',
                ),
                spotlight_url: FakeResponse(
                    url=spotlight_url,
                    text='<a href="/virtual/2025/poster/2">Official Spotlight Paper</a>',
                ),
            }
        )
        source = NeurIPSSource(self.config, session=cast(requests.Session, session))
        mapping = source._presentation_map(2025)
        self.assertEqual(
            mapping["spotlight attention for kv cache retrieval"], "Poster"
        )
        self.assertEqual(mapping["official spotlight paper"], "Spotlight")
        self.assertEqual(mapping["official oral paper"], "Oral")

    def test_openalex_accepts_configured_top_venue(self):
        source = OpenAlexSource(
            self.config, session=cast(requests.Session, FakeSession(FakeResponse()))
        )
        paper = source._paper_from_work(
            {
                "id": "https://openalex.org/W1",
                "title": "Efficient Agent Memory",
                "publication_date": "2026-08-10",
                "cited_by_count": 7,
                "abstract_inverted_index": {"Agent": [0], "memory": [1]},
                "primary_location": {
                    "landing_page_url": "https://example.test/paper",
                    "source": {
                        "display_name": "International Conference on Machine Learning"
                    },
                },
                "authorships": [{"author": {"display_name": "Ada"}}],
            },
            "cot_agentic_ai",
        )
        self.assertIsNotNone(paper)
        assert paper is not None
        self.assertEqual(paper["venue_tags"], ["ICML"])
        self.assertEqual(paper["summary"], "Agent memory")

    def test_historical_openalex_keeps_old_cited_or_top_conference_papers(self):
        works = [
            {
                "id": "https://openalex.org/W1",
                "title": "Foundational Agent Memory",
                "publication_date": "2022-01-01",
                "cited_by_count": 240,
                "abstract_inverted_index": {"Agent": [0], "memory": [1]},
                "primary_location": {
                    "landing_page_url": "https://example.test/w1",
                    "source": {"display_name": "Unlisted Workshop"},
                },
            },
            {
                "id": "https://openalex.org/W2",
                "title": "Top Venue Tool Use",
                "publication_date": "2023-01-01",
                "cited_by_count": 4,
                "abstract_inverted_index": {"Tool": [0], "use": [1]},
                "primary_location": {
                    "landing_page_url": "https://example.test/w2",
                    "source": {
                        "display_name": "International Conference on Learning Representations"
                    },
                },
            },
            {
                "id": "https://openalex.org/W3",
                "title": "Low Impact Agent Note",
                "publication_date": "2021-01-01",
                "cited_by_count": 3,
                "abstract_inverted_index": {"Agent": [0]},
                "primary_location": {
                    "landing_page_url": "https://example.test/w3",
                    "source": {"display_name": "Unlisted Workshop"},
                },
            },
        ]
        session = FakeSession(FakeResponse(payload={"results": works}))
        source = HistoricalOpenAlexSource(
            self.config, session=cast(requests.Session, session)
        )
        source.interval = 0
        papers = source.fetch(date(2026, 8, 12), "cot_agentic_ai")
        self.assertEqual(
            [paper["source_id"] for paper in papers],
            [
                "https://openalex.org/W1",
                "https://openalex.org/W2",
            ],
        )
        self.assertEqual(papers[1]["venue_tags"], ["ICLR"])
        self.assertGreaterEqual(session.calls, 2)

    def test_historical_openalex_builds_separate_citation_and_venue_queries(self):
        session = FakeSession(FakeResponse(payload={"results": []}))
        source = HistoricalOpenAlexSource(
            self.config, session=cast(requests.Session, session)
        )
        source.interval = 0
        captured: list[dict] = []
        original_get = session.get

        def recording_get(url, **kwargs):
            captured.append(kwargs["params"])
            return original_get(url, **kwargs)

        session.get = recording_get
        source.fetch(date(2026, 8, 12), "cot_agentic_ai")
        filters = [str(params["filter"]) for params in captured]
        self.assertTrue(any("cited_by_count" in value for value in filters))
        self.assertTrue(
            any(
                "primary_location.source.display_name.search" in value
                for value in filters
            )
        )
        venue_filter = next(
            value
            for value in filters
            if "primary_location.source.display_name.search" in value
        )
        self.assertIn(
            "international conference on learning representations", venue_filter
        )
        self.assertNotIn("+", venue_filter)

    def test_briefs_do_not_request_figures(self):
        config = deepcopy(self.config)
        config["sources"]["arxiv_html"]["base_url"] = "https://unreachable.invalid"
        papers = [
            {
                "arxiv_id": "2608.00001",
                "content_tier": "brief",
                "figure_url": "stale.png",
            }
        ]
        result = enrich_selected_figures(papers, config, date(2026, 8, 10))
        self.assertIsNone(result[0]["figure_url"])
        self.assertEqual(result[0]["figure_status"], "not_requested")


if __name__ == "__main__":
    unittest.main()
