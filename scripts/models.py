"""Shared data models for the GitHub trend radar."""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


FULL_NAME_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?/[a-z0-9._-]{1,100}"
)


@dataclass
class RepositoryRecord:
    """Normalized repository data collected from one or more sources."""

    full_name: str
    repo_url: str = ""
    description: str = ""
    repository_type: str = ""
    primary_category: str = ""
    secondary_tags: List[str] = field(default_factory=list)
    classification_evidence: List[str] = field(default_factory=list)
    total_stars: Optional[int] = None
    stars_24h_external: Optional[int] = None
    stars_7d_external: Optional[int] = None
    stars_30d_external: Optional[int] = None
    stars_24h_local: Optional[int] = None
    stars_7d_local: Optional[int] = None
    growth_acceleration: Optional[float] = None
    forks: Optional[int] = None
    primary_language: str = ""
    topics: List[str] = field(default_factory=list)
    license: str = ""
    created_at: str = ""
    pushed_at: str = ""
    latest_release: str = ""
    latest_release_at: str = ""
    archived: bool = False
    open_issues: Optional[int] = None
    source_records: List[Dict[str, Any]] = field(default_factory=list)
    data_confidence: str = ""

    def __post_init__(self) -> None:
        normalized_full_name = self.full_name.strip().lower()
        if FULL_NAME_PATTERN.fullmatch(normalized_full_name) is None:
            raise ValueError("full_name must use the 'owner/repo' format")
        if normalized_full_name.rsplit("/", 1)[-1] in {".", ".."}:
            raise ValueError("full_name must use the 'owner/repo' format")

        self.full_name = normalized_full_name
        if not self.repo_url and "/" in self.full_name:
            self.repo_url = f"https://github.com/{self.full_name}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
