"""Fetch and normalize repository metadata and README summaries."""

import html
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import quote, urlencode

from http_client import SourceError, get_json, get_text, github_token
from models import RepositoryRecord


GITHUB_API = "https://api.github.com"
RAW_GITHUB = "https://raw.githubusercontent.com"

_FEATURE_HEADINGS = {"features", "feature", "特性", "功能", "核心功能", "主要功能"}
_USE_CASE_HEADINGS = {
    "use cases", "use case", "applications", "application scenarios",
    "应用场景", "使用场景", "典型场景",
}


def _clean_readme_bullet(value: str) -> str:
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", "", value)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip(" -—")


def parse_readme_sections(text: str) -> Dict[str, List[str]]:
    """Extract concise feature and use-case bullets from Markdown README text."""

    if not isinstance(text, str):
        raise ValueError("README must be text")
    result: Dict[str, List[str]] = {"features": [], "use_cases": []}
    active = None
    active_level = None
    for raw_line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", raw_line)
        if heading:
            level = len(heading.group(1))
            title = _clean_readme_bullet(heading.group(2)).casefold()
            if any(title == name or title.endswith(" " + name) for name in _FEATURE_HEADINGS):
                active, active_level = "features", level
            elif any(title == name or title.endswith(" " + name) for name in _USE_CASE_HEADINGS):
                active, active_level = "use_cases", level
            elif active is not None and level <= active_level:
                active, active_level = None, None
            continue
        if active is None or len(result[active]) >= 8:
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.+?)\s*$", raw_line)
        if not bullet:
            continue
        cleaned = _clean_readme_bullet(bullet.group(1))
        if cleaned and cleaned not in result[active]:
            result[active].append(cleaned)
    return result


def fetch_readme_sections(full_name: str) -> Dict[str, List[str]]:
    """Fetch a repository README, preferring common Chinese variants."""

    validated = RepositoryRecord(full_name=full_name)
    paths = (
        "docs/README-zh.md", "README.zh-CN.md", "README_zh.md", "README.md",
    )
    for branch in ("main", "master"):
        for path in paths:
            url = f"{RAW_GITHUB}/{validated.full_name}/{branch}/{path}"
            try:
                sections = parse_readme_sections(
                    get_text(url, headers={"User-Agent": "github-trend-radar"}, timeout=8)
                )
            except SourceError:
                continue
            if sections["features"] or sections["use_cases"]:
                sections["source"] = [f"README:{branch}/{path}"]
                return sections
    raise SourceError("GitHub README sections were unavailable")


def _integer(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _items(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        rows = payload.get("items", [])
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    return (row for row in rows if isinstance(row, Mapping))


def _license_name(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    identifier = value.get("spdx_id")
    if identifier and identifier != "NOASSERTION":
        return str(identifier)
    return str(value.get("name") or "")


def _topics(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(topic) for topic in value]


def parse_search_rows(payload: Any) -> List[RepositoryRecord]:
    """Convert GitHub search rows to normalized repository records."""

    records: List[RepositoryRecord] = []
    for row in _items(payload):
        full_name = str(row.get("full_name") or "").strip()
        try:
            record = RepositoryRecord(
                full_name=full_name,
                repo_url=str(row.get("html_url") or ""),
                description=str(row.get("description") or ""),
                total_stars=_integer(row.get("stargazers_count")),
                forks=_integer(row.get("forks_count")),
                primary_language=str(row.get("language") or ""),
                topics=_topics(row.get("topics")),
                license=_license_name(row.get("license")),
                created_at=str(row.get("created_at") or ""),
                pushed_at=str(row.get("pushed_at") or ""),
                archived=row.get("archived") is True,
                open_issues=_integer(row.get("open_issues_count")),
                source_records=[{"source": "github", "scope": "search"}],
            )
        except ValueError:
            continue
        records.append(record)
    return records


def _headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-trend-radar",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_top_repositories(
    top: int = 100, query: str = "stars:>0"
) -> List[RepositoryRecord]:
    """Fetch the highest-starred repositories matching a GitHub query."""

    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 100:
        raise ValueError("top must be an integer between 1 and 100")
    parameters = urlencode(
        {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": top,
        }
    )
    payload = get_json(f"{GITHUB_API}/search/repositories?{parameters}", headers=_headers())
    return parse_search_rows(payload)


def fetch_repo_details(full_name: str) -> RepositoryRecord:
    """Fetch one repository and enrich it with its latest release, when present."""

    validated = RepositoryRecord(full_name=full_name)
    repository_path = quote(validated.full_name, safe="/")
    headers = _headers()
    payload = get_json(f"{GITHUB_API}/repos/{repository_path}", headers=headers)
    records = parse_search_rows({"items": [payload]})
    if not records:
        raise SourceError("GitHub repository response was unusable")
    record = records[0]

    release_status = "release_fetch_failed"
    release_record: Dict[str, Any] = {
        "source": "github_latest_release",
        "status": release_status,
        "status_code": None,
    }
    try:
        release = get_json(
            f"{GITHUB_API}/repos/{repository_path}/releases/latest", headers=headers
        )
    except SourceError as exc:
        release = None
        safe_status_code = (
            exc.status_code
            if isinstance(exc.status_code, int) and not isinstance(exc.status_code, bool)
            else None
        )
        if safe_status_code == 404:
            release_status = "none_or_unavailable"
        release_record = {
            "source": "github_latest_release",
            "status": release_status,
            "status_code": safe_status_code,
        }

    if isinstance(release, Mapping) and release.get("tag_name"):
        record.latest_release = str(release["tag_name"])
        record.latest_release_at = str(
            release.get("published_at") or release.get("created_at") or ""
        )
        release_status = "available"
        release_record = {
            "source": "github_latest_release",
            "status": release_status,
        }

    record.source_records[0]["scope"] = "repository_detail"
    record.source_records[0]["release"] = release_status
    record.source_records.append(release_record)
    return record


def fetch_repo_star_count(full_name: str) -> RepositoryRecord:
    """Read public page metadata when REST detail quota is unavailable."""
    validated = RepositoryRecord(full_name=full_name)
    repository_path = quote(validated.full_name, safe="/")
    page = get_text(
        f"https://github.com/{repository_path}",
        headers={"User-Agent": "github-trend-radar"},
        timeout=12,
    )
    match = re.search(
        r'aria-label="([0-9][0-9,]*) users? starred this repository"',
        page,
    )
    if not match:
        raise SourceError("GitHub repository page star count was unavailable")
    total_stars = _integer(match.group(1))
    if total_stars is None:
        raise SourceError("GitHub repository page star count was unusable")
    return RepositoryRecord(
        full_name=validated.full_name,
        repo_url=f"https://github.com/{validated.full_name}",
        total_stars=total_stars,
        source_records=[{
            "source": "github_web",
            "scope": "repository_star_fallback",
        }],
    )
