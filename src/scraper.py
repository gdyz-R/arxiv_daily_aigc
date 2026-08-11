"""Backward-compatible crawling facade."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

try:
    from config import load_config
    from crawl import crawl_papers
except ImportError:  # pragma: no cover
    from .config import load_config
    from .crawl import crawl_papers


def fetch_cv_papers(
    category: str = "cs.CV",
    max_results: int = 500,
    specified_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Use the new multi-source crawler; old arguments remain source-compatible."""
    del category, max_results
    return crawl_papers(specified_date or date.today(), load_config())
