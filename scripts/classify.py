"""Merge repositories and classify them with configurable, traceable rules."""

from copy import deepcopy
from dataclasses import fields
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config import validate_category_rules
from models import RepositoryRecord


PROTECTED_METRICS = {
    "total_stars",
    "stars_24h_external",
    "stars_7d_external",
    "stars_30d_external",
    "stars_24h_local",
    "stars_7d_local",
    "growth_acceleration",
}
LOCAL_METRICS = {
    "stars_24h_local",
    "stars_7d_local",
    "growth_acceleration",
}
NUMERIC_FIELDS = PROTECTED_METRICS | {"forks", "open_issues"}
ORDERED_LIST_FIELDS = {
    "topics",
    "secondary_tags",
    "classification_evidence",
}
NON_INFORMATIVE_STRINGS = {"其他", "低"}
SOURCE_PRIORITY = {
    "github": 0,
    "ossinsight": 1,
    "github_trending": 2,
}
EXTERNAL_SOURCE_PRIORITY = {
    "ossinsight": 0,
    "github_trending": 1,
    "github": 2,
}
FIELD_PROVENANCE_KEY = "_field_provenance"


def _ordered_union(existing: Sequence[Any], incoming: Sequence[Any]) -> List[Any]:
    combined: List[Any] = []
    for item in [*existing, *incoming]:
        if item not in combined:
            combined.append(deepcopy(item))
    return combined


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _raw_source_records(record: RepositoryRecord) -> List[Dict[str, Any]]:
    return [
        item
        for item in record.source_records
        if isinstance(item, dict) and FIELD_PROVENANCE_KEY not in item
    ]


def _stored_field_provenance(record: RepositoryRecord) -> Mapping[str, Any]:
    for item in record.source_records:
        if isinstance(item, Mapping) and isinstance(
            item.get(FIELD_PROVENANCE_KEY), Mapping
        ):
            return item[FIELD_PROVENANCE_KEY]
    return {}


def _record_key(record: RepositoryRecord) -> str:
    payload = record.to_dict()
    payload["source_records"] = sorted(
        _raw_source_records(record), key=_canonical
    )
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _infer_field_meta(record: RepositoryRecord, field_name: str) -> Dict[str, Any]:
    sources = sorted(
        {
            str(item.get("source", "")).strip().lower()
            for item in _raw_source_records(record)
            if str(item.get("source", "")).strip()
        }
    )
    if field_name in LOCAL_METRICS:
        source = "local_history"
        priority = 0
    else:
        priorities = (
            EXTERNAL_SOURCE_PRIORITY
            if field_name.endswith("_external")
            else SOURCE_PRIORITY
        )
        source = min(
            sources or ["unknown"],
            key=lambda name: (priorities.get(name, 99), name),
        )
        priority = priorities.get(source, 99)
    return {
        "source": source,
        "priority": priority,
        "pushed_at": record.pushed_at,
        "record_key": _record_key(record),
    }


def _field_meta(record: RepositoryRecord, field_name: str) -> Dict[str, Any]:
    stored = _stored_field_provenance(record).get(field_name)
    if isinstance(stored, Mapping) and "source" in stored:
        return deepcopy(dict(stored))
    return _infer_field_meta(record, field_name)


def _scalar_candidates(
    records: Sequence[RepositoryRecord], field_name: str
) -> List[Tuple[Any, Dict[str, Any]]]:
    return [
        (deepcopy(getattr(record, field_name)), _field_meta(record, field_name))
        for record in records
        if getattr(record, field_name) is not None
        and not (
            isinstance(getattr(record, field_name), str)
            and not getattr(record, field_name).strip()
        )
    ]


def _choose_numeric(
    records: Sequence[RepositoryRecord], field_name: str
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    candidates = _scalar_candidates(records, field_name)
    if not candidates:
        return None, None
    best_priority = min(meta["priority"] for _, meta in candidates)
    candidates = [
        candidate for candidate in candidates if candidate[1]["priority"] == best_priority
    ]
    value, meta = max(
        candidates,
        key=lambda candidate: (
            candidate[0],
            candidate[1].get("pushed_at", ""),
            candidate[1].get("source", ""),
            candidate[1].get("record_key", ""),
        ),
    )
    return value, meta


def _choose_string(
    records: Sequence[RepositoryRecord], field_name: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    candidates = _scalar_candidates(records, field_name)
    concrete = [
        candidate
        for candidate in candidates
        if str(candidate[0]).strip() not in NON_INFORMATIVE_STRINGS
    ]
    if concrete:
        candidates = concrete
    if not candidates:
        return "", None
    best_priority = min(meta["priority"] for _, meta in candidates)
    candidates = [
        candidate for candidate in candidates if candidate[1]["priority"] == best_priority
    ]
    value, meta = max(
        candidates,
        key=lambda candidate: (
            candidate[1].get("pushed_at", ""),
            str(candidate[0]).casefold(),
            str(candidate[0]),
            candidate[1].get("record_key", ""),
        ),
    )
    return deepcopy(value), meta


def _list_candidates(
    record: RepositoryRecord, field_name: str
) -> List[Tuple[Any, Dict[str, Any]]]:
    value = getattr(record, field_name)
    stored = _stored_field_provenance(record).get(field_name)
    if isinstance(stored, list):
        stored_by_value = {
            _canonical(item.get("value")): item
            for item in stored
            if isinstance(item, Mapping) and "value" in item
        }
        if all(_canonical(item) in stored_by_value for item in value):
            return [
                (
                    deepcopy(item),
                    {
                        key: deepcopy(meta_value)
                        for key, meta_value in stored_by_value[
                            _canonical(item)
                        ].items()
                        if key != "value"
                    },
                )
                for item in value
            ]

    base_meta = _infer_field_meta(record, field_name)
    return [
        (
            deepcopy(item),
            {**deepcopy(base_meta), "position": position},
        )
        for position, item in enumerate(value)
    ]


def _meta_is_better(candidate: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    candidate_priority = int(candidate.get("priority", 99))
    current_priority = int(current.get("priority", 99))
    if candidate_priority != current_priority:
        return candidate_priority < current_priority
    candidate_pushed = str(candidate.get("pushed_at", ""))
    current_pushed = str(current.get("pushed_at", ""))
    if candidate_pushed != current_pushed:
        return candidate_pushed > current_pushed
    return (
        str(candidate.get("record_key", "")), int(candidate.get("position", 0))
    ) < (
        str(current.get("record_key", "")), int(current.get("position", 0))
    )


def _choose_list(
    records: Sequence[RepositoryRecord], field_name: str
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    selected: Dict[str, Tuple[Any, Dict[str, Any]]] = {}
    for record in records:
        for value, meta in _list_candidates(record, field_name):
            key = _canonical(value)
            if key not in selected or _meta_is_better(meta, selected[key][1]):
                selected[key] = value, meta
    ordered = list(selected.values())
    ordered.sort(
        key=lambda candidate: (
            candidate[1].get("record_key", ""),
            int(candidate[1].get("position", 0)),
            _canonical(candidate[0]),
        )
    )
    ordered.sort(key=lambda candidate: candidate[1].get("pushed_at", ""), reverse=True)
    ordered.sort(key=lambda candidate: int(candidate[1].get("priority", 99)))
    values = [deepcopy(value) for value, _ in ordered]
    provenance = [
        {"value": deepcopy(value), **deepcopy(meta)} for value, meta in ordered
    ]
    return values, provenance


def _merge_group(records: Sequence[RepositoryRecord]) -> RepositoryRecord:
    full_name = records[0].full_name.strip().lower()
    result = RepositoryRecord(full_name=full_name)
    field_provenance: Dict[str, Any] = {}

    for field_info in fields(RepositoryRecord):
        name = field_info.name
        if name in {"full_name", "source_records", "archived"}:
            continue
        if name in NUMERIC_FIELDS:
            value, provenance = _choose_numeric(records, name)
            if value is not None:
                setattr(result, name, value)
                field_provenance[name] = deepcopy(provenance)
        elif name in ORDERED_LIST_FIELDS:
            value, provenance = _choose_list(records, name)
            setattr(result, name, value)
            if value:
                field_provenance[name] = provenance
        elif isinstance(getattr(result, name), str):
            value, provenance = _choose_string(records, name)
            if value:
                setattr(result, name, value)
                field_provenance[name] = deepcopy(provenance)

    result.archived = any(record.archived for record in records)
    field_provenance["archived"] = {"source": "aggregate", "strategy": "or"}
    provenance: List[Dict[str, Any]] = []
    all_provenance = [
        item
        for record in records
        for item in _raw_source_records(record)
    ]
    for item in sorted(
        all_provenance,
        key=lambda value: (
            SOURCE_PRIORITY.get(str(value.get("source", "")).strip().lower(), 3),
            _canonical(value),
        ),
    ):
        if item not in provenance:
            provenance.append(deepcopy(item))
    provenance.append(
        {"source": "merge", FIELD_PROVENANCE_KEY: field_provenance}
    )
    result.source_records = provenance
    return result


def merge_records(records: Iterable[RepositoryRecord]) -> List[RepositoryRecord]:
    """Merge by name with deterministic source authority and value selection."""

    grouped: Dict[str, List[RepositoryRecord]] = {}
    for record in records:
        key = record.full_name.strip().lower()
        grouped.setdefault(key, []).append(record)
    return [_merge_group(grouped[key]) for key in sorted(grouped)]


def _contains_term(text: str, term: str) -> bool:
    normalized_term = str(term).strip().lower()
    if not normalized_term:
        return False
    left = r"(?<![a-z0-9])" if normalized_term[0].isalnum() else ""
    right = r"(?![a-z0-9])" if normalized_term[-1].isalnum() else ""
    return re.search(f"{left}{re.escape(normalized_term)}{right}", text) is not None


def detect_repository_type(record: RepositoryRecord) -> str:
    """Infer the repository's material/software type using explicit precedence."""

    description = record.description.lower()
    repo_name = record.full_name.rsplit("/", 1)[-1].lower()
    topics = {str(topic).strip().lower() for topic in record.topics}

    def explicit(*terms: str) -> bool:
        return any(
            term in topics
            or re.search(
                rf"(?:^|[-_.]){re.escape(term)}(?:s)?(?:$|[-_.])", repo_name
            )
            is not None
            for term in terms
        )

    if explicit("awesome", "awesome-list") or re.search(
        r"\b(?:curated\s+(?:list\s+of\s+)?awesome|awesome\s+list)\b",
        description,
    ):
        return "Awesome 清单"
    if explicit("tutorial", "course") or re.search(
        r"\b(?:tutorials?|(?:online\s+)?course\s+materials?)\b", description
    ):
        return "教程或课程"
    if explicit("book", "handbook", "guide") or re.search(
        r"\b(?:books?|handbook|guide)\b", description
    ):
        return "书籍或资料"
    if explicit("dataset", "corpus") or re.search(
        r"\b(?:datasets?|data\s+sets?|corpus)\b", description
    ):
        return "数据集"
    if explicit("model") or re.search(
        r"\b(?:pretrained\s+models?|model\s+weights)\b", description
    ):
        return "模型"
    if explicit("template") or re.search(
        r"\b(?:repository|project|starter)\s+templates?\b", description
    ):
        return "模板"
    if explicit("sdk", "library", "libraries", "framework", "toolkit") or re.search(
        r"\b(?:sdks?|libraries|library|frameworks?|toolkits?)\b", description
    ):
        return "SDK 或库"
    return "可运行软件"


def _as_terms(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list) and all(
        isinstance(item, str) and item.strip() for item in value
    ):
        return list(value)
    raise ValueError("规则匹配项必须是 str 或 list[str]")


def _rule_terms(rule: Any, source: str) -> List[str]:
    if not isinstance(rule, Mapping):
        return _as_terms(rule) if source == "keywords" else []
    aliases = {
        "collections": (
            "ossinsight_collections",
            "collection_names",
            "collections",
            "ossinsight",
        ),
        "topics": ("topics", "github_topics"),
        "keywords": ("keywords", "keyword"),
    }
    terms: List[str] = []
    for key in aliases[source]:
        if key in rule:
            terms = _ordered_union(terms, _as_terms(rule[key]))
    return terms


def _exact_match(values: Sequence[str], terms: Sequence[str]) -> Optional[str]:
    normalized_values = {str(value).strip().lower() for value in values}
    for term in terms:
        if term.strip().lower() in normalized_values:
            return term
    return None


def _keyword_match(text: str, terms: Sequence[str]) -> Optional[str]:
    for term in terms:
        if _contains_term(text, term):
            return term
    return None


def _match_rule(
    rule: Any,
    collection_names: Sequence[str],
    topics: Sequence[str],
    keyword_text: str,
) -> Optional[Tuple[str, str]]:
    collection_match = _exact_match(collection_names, _rule_terms(rule, "collections"))
    if collection_match is not None:
        return "OSSInsight collection_names", collection_match
    topic_match = _exact_match(topics, _rule_terms(rule, "topics"))
    if topic_match is not None:
        return "GitHub topics", topic_match
    keyword_match = _keyword_match(keyword_text, _rule_terms(rule, "keywords"))
    if keyword_match is not None:
        return "关键词", keyword_match
    return None


def _source_collections(record: RepositoryRecord) -> List[str]:
    collections: List[str] = []
    for source_record in record.source_records:
        if not isinstance(source_record, Mapping):
            continue
        for key in ("collection_names", "collection_name", "collection"):
            if key in source_record:
                collections = _ordered_union(
                    collections, _as_terms(source_record[key])
                )
    dynamic_collections = getattr(record, "collection_names", None)
    if dynamic_collections is not None:
        collections = _ordered_union(collections, _as_terms(dynamic_collections))
    return collections


def _append_evidence(
    record: RepositoryRecord,
    target: str,
    label: str,
    source: str,
    matched: str,
) -> None:
    evidence = f"{target}：{label}；来源：{source}；匹配：{matched}"
    if evidence not in record.classification_evidence:
        record.classification_evidence.append(evidence)


def _manual_override(rules: Mapping[str, Any], full_name: str) -> Optional[Any]:
    overrides = rules.get("人工覆盖", {})
    if not isinstance(overrides, Mapping):
        return None
    normalized_name = full_name.strip().lower()
    for name, override in overrides.items():
        if str(name).strip().lower() == normalized_name:
            return override
    return None


def classify_repository(
    record: RepositoryRecord,
    rules: Mapping[str, Any],
    collection_names: Optional[Sequence[str]] = None,
) -> RepositoryRecord:
    """Classify one repository and record the exact rule source used."""

    rules = validate_category_rules(rules, "<classify_repository>")
    primary_rules = rules.get("一级分类")
    secondary_rules = rules.get("二级标签")

    record.primary_category = "其他"
    record.secondary_tags = []
    record.classification_evidence = []
    record.repository_type = detect_repository_type(record)
    manual = _manual_override(rules, record.full_name)
    if isinstance(manual, Mapping):
        record.primary_category = manual["一级分类"]
        record.secondary_tags = _ordered_union([], manual.get("二级标签", []))
        _append_evidence(
            record,
            "一级分类",
            record.primary_category,
            "人工覆盖",
            record.full_name,
        )
        return record

    collections = _source_collections(record)
    if collection_names is not None:
        collections = _ordered_union(collections, _as_terms(collection_names))
    topics = [str(topic) for topic in record.topics]
    keyword_text = " ".join([record.full_name, record.description, *topics]).lower()

    primary_match: Optional[Tuple[str, str, str]] = None
    for source in ("collections", "topics", "keywords"):
        for category, rule in primary_rules.items():
            if str(category) == "其他":
                continue
            terms = _rule_terms(rule, source)
            matched = (
                _exact_match(collections, terms)
                if source == "collections"
                else _exact_match(topics, terms)
                if source == "topics"
                else _keyword_match(keyword_text, terms)
            )
            if matched is not None:
                source_label = {
                    "collections": "OSSInsight collection_names",
                    "topics": "GitHub topics",
                    "keywords": "关键词",
                }[source]
                primary_match = str(category), source_label, matched
                break
        if primary_match is not None:
            break

    if primary_match is None:
        record.primary_category = "其他"
    else:
        category, source, matched = primary_match
        record.primary_category = category
        _append_evidence(record, "一级分类", category, source, matched)

    for tag, rule in secondary_rules.items():
        match = _match_rule(rule, collections, topics, keyword_text)
        if match is None:
            continue
        tag_name = str(tag)
        if tag_name not in record.secondary_tags:
            record.secondary_tags.append(tag_name)
        source, matched = match
        _append_evidence(record, "二级标签", tag_name, source, matched)
    return record
