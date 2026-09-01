"""Evidence-gated analysis of potential repository replacement relations."""

import copy
import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Dict, List, Set, Union

from models import RepositoryRecord


DIRECT_EVIDENCE = frozenset(
    {
        "migration_guide",
        "official_comparison",
        "release",
        "issue",
        "community_discussion",
    }
)

_CHINESE_EVIDENCE_WHITELIST = {
    "迁移说明": "migration_guide",
    "官方对比": "official_comparison",
    "社区讨论": "community_discussion",
}

_ASCII_EVIDENCE_WHITELIST = {
    "release": "release",
    "issue": "issue",
}

_WORD_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = frozenset(
    {"a", "an", "and", "for", "from", "in", "of", "on", "the", "to", "with"}
)


def _normalized_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"repository {field_name} must be text")
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _normalized_tags(repo: RepositoryRecord) -> Set[str]:
    if not isinstance(repo.secondary_tags, list):
        raise ValueError("repository secondary_tags must be a list")
    tags = set()
    for value in repo.secondary_tags:
        tag = _normalized_text(value, "secondary tag")
        if tag:
            tags.add(tag)
    return tags


def _description_words(repo: RepositoryRecord) -> Set[str]:
    description = _normalized_text(repo.description, "description")
    return {
        word
        for word in _WORD_PATTERN.findall(description)
        if len(word) >= 2 and word not in _STOP_WORDS
    }


def _growth_value(repo: RepositoryRecord) -> Union[int, float]:
    value = (
        repo.stars_7d_external
        if repo.stars_7d_external is not None
        else repo.stars_7d_local
    )
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("repository growth must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("repository growth must be a finite number")
    return value


def _canonical_evidence_type(value: str) -> Union[str, None]:
    stripped = value.strip()
    chinese_type = _CHINESE_EVIDENCE_WHITELIST.get(stripped)
    if chinese_type is not None:
        return chinese_type
    if re.fullmatch(r"[A-Za-z]+", stripped) is None:
        return None
    return _ASCII_EVIDENCE_WHITELIST.get(stripped.lower())


def _normalize_evidence(evidence: Any) -> List[Dict[str, Any]]:
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")

    normalized_items = []
    for item in evidence:
        if type(item) is not dict:
            raise ValueError("each evidence item must be a mapping")
        evidence_type = item.get("type")
        if type(evidence_type) is not str:
            raise ValueError("evidence type must be supported text")

        normalized_item = copy.deepcopy(item)
        canonical_type = _canonical_evidence_type(normalized_item["type"])
        if canonical_type is None:
            raise ValueError("evidence type is unsupported")

        normalized_item["type"] = canonical_type
        normalized_items.append(normalized_item)
    return normalized_items


def analyze_replacement(
    old_repo: RepositoryRecord,
    new_repo: RepositoryRecord,
    evidence: List[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Classify whether ``new_repo`` is replacing attention or capability.

    A direct-replacement conclusion requires an explicit evidence type from
    ``DIRECT_EVIDENCE``. Similarity and growth alone can produce no stronger
    than a partial-replacement conclusion.
    """
    if not isinstance(old_repo, RepositoryRecord) or not isinstance(
        new_repo, RepositoryRecord
    ):
        raise ValueError("repositories must be RepositoryRecord instances")

    normalized_evidence = _normalize_evidence(evidence)
    old_category = _normalized_text(old_repo.primary_category, "primary category")
    new_category = _normalized_text(new_repo.primary_category, "primary category")
    same_category = (
        bool(old_category)
        and old_category == new_category
        and old_category not in {"其他", "other"}
    )

    overlap_tags = sorted(_normalized_tags(old_repo) & _normalized_tags(new_repo))
    scenario_overlap = (
        bool(_description_words(old_repo) & _description_words(new_repo))
        or len(overlap_tags) >= 2
    )
    higher_growth = _growth_value(new_repo) > _growth_value(old_repo)
    has_direct_evidence = any(
        item["type"] in DIRECT_EVIDENCE for item in normalized_evidence
    )

    if not same_category:
        relation, confidence = "无明确替代者", "低"
    elif (
        len(overlap_tags) >= 2
        and scenario_overlap
        and higher_growth
        and has_direct_evidence
    ):
        relation, confidence = "直接替代", "高"
    elif len(overlap_tags) >= 2 and scenario_overlap and higher_growth:
        relation, confidence = "部分替代", "中"
    elif higher_growth:
        relation, confidence = "注意力分流", "中低"
    else:
        relation, confidence = "同赛道共存", "低"

    return {
        "relation": relation,
        "confidence": confidence,
        "overlap_tags": overlap_tags,
        "evidence": normalized_evidence,
    }
