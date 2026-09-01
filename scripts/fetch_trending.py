"""Parse GitHub Trending HTML using only the Python standard library."""

import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from http_client import get_text
from models import RepositoryRecord


TRENDING_URL = "https://github.com/trending"
PERIOD_FIELDS = {
    "daily": "stars_24h_external",
    "weekly": "stars_7d_external",
    "monthly": "stars_30d_external",
}
REPOSITORY_HREF = re.compile(r"^/([^/\s]+)/([^/\s]+?)/?$")
TREND_COUNTS = {
    "daily": re.compile(r"([\d,]+)\s+stars?\s+today", re.IGNORECASE),
    "weekly": re.compile(r"([\d,]+)\s+stars?\s+this\s+week", re.IGNORECASE),
    "monthly": re.compile(r"([\d,]+)\s+stars?\s+this\s+month", re.IGNORECASE),
}


def _integer(text: str) -> Optional[int]:
    match = re.search(r"[\d,]+", text)
    if match is None:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


class _TrendingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None
        self._star_anchor = False
        self._language = False
        self._description = False
        self._title = False
        self._trend_count = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attributes = dict(attrs)
        if tag == "article" and self.current is None:
            classes = (attributes.get("class") or "").split()
            if "Box-row" in classes:
                self.current = {
                    "full_name": "",
                    "star_text": [],
                    "language_text": [],
                    "description_text": [],
                    "trend_text": [],
                }
            return
        if self.current is None:
            return

        if tag == "h2":
            self._title = True
        elif tag == "a":
            href = attributes.get("href") or ""
            repository_match = REPOSITORY_HREF.fullmatch(href)
            if self._title and repository_match and not self.current["full_name"]:
                self.current["full_name"] = "/".join(repository_match.groups())
            if href.rstrip("/").endswith("/stargazers"):
                self._star_anchor = True
        elif tag == "span":
            if attributes.get("itemprop") == "programmingLanguage":
                self._language = True
            classes = set((attributes.get("class") or "").split())
            if {"d-inline-block", "float-sm-right"}.issubset(classes):
                self._trend_count = True
        elif tag == "p":
            self._description = True

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag == "a":
            self._star_anchor = False
        elif tag == "h2":
            self._title = False
        elif tag == "span":
            self._language = False
            self._trend_count = False
        elif tag == "p":
            self._description = False
        elif tag == "article":
            self.rows.append(self.current)
            self.current = None
            self._star_anchor = False
            self._language = False
            self._description = False
            self._title = False
            self._trend_count = False

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        if self._star_anchor:
            self.current["star_text"].append(data)
        if self._language:
            self.current["language_text"].append(data)
        if self._description:
            self.current["description_text"].append(data)
        if self._trend_count:
            self.current["trend_text"].append(data)


def _clean(parts: List[str]) -> str:
    return " ".join(" ".join(parts).split())


def parse_trending_html(html: str, period: str = "weekly") -> List[RepositoryRecord]:
    """Parse genuine ``article.Box-row`` entries without inventing fallback rows."""

    if period not in PERIOD_FIELDS:
        raise ValueError("period must be one of: daily, weekly, monthly")
    parser = _TrendingParser()
    try:
        parser.feed(html or "")
        parser.close()
    except (TypeError, ValueError):
        return []

    records: List[RepositoryRecord] = []
    metric_field = PERIOD_FIELDS[period]
    for row in parser.rows:
        full_name = row["full_name"]
        total_stars = _integer(_clean(row["star_text"]))
        trend_match = TREND_COUNTS[period].fullmatch(_clean(row["trend_text"]))
        trend_stars = _integer(trend_match.group(1)) if trend_match else None
        if not full_name or total_stars is None or trend_stars is None:
            continue
        try:
            record = RepositoryRecord(
                full_name=full_name,
                description=_clean(row["description_text"]),
                total_stars=total_stars,
                primary_language=_clean(row["language_text"]),
                source_records=[
                    {
                        "source": "github_trending",
                        "scope": "selected_set",
                        "period": period,
                    }
                ],
                **{metric_field: trend_stars},
            )
        except ValueError:
            continue
        records.append(record)
    return records


def fetch_trending(
    period: str = "weekly", language: str = ""
) -> List[RepositoryRecord]:
    """Fetch and parse one GitHub Trending page."""

    if period not in PERIOD_FIELDS:
        raise ValueError("period must be one of: daily, weekly, monthly")
    base_url = TRENDING_URL
    if language:
        base_url += "/" + quote(language.strip().lower(), safe="")
    url = f"{base_url}?{urlencode({'since': period})}"
    html = get_text(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "github-trend-radar",
        },
    )
    return parse_trending_html(html, period)
