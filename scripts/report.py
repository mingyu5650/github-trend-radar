"""Render the canonical GitHub trend model as a safe Chinese Markdown report."""

import html
import re
import unicodedata
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from replacement import DIRECT_EVIDENCE


REPORT_TITLE = "GitHub 趋势雷达日报"
SECTION_TITLES = (
    "今日速览",
    "核心排行榜",
    "分类趋势",
    "可复用项目榜",
    "重点项目解读",
    "历史热门、退榜与替代",
    "市场走向",
    "个人观察清单",
    "数据口径与异常",
)

_REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9._-]{1,100}"
)
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_PATTERN = re.compile(r"[\r\n\t]+")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|[_-])(?:api[_-]?)?(?:token|authorization|headers?|cookies?|"
    r"password|passwd|secret)(?:$|[_-])",
    re.IGNORECASE,
)
_MODEL_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_MAX_MODEL_DEPTH = 64
_MAX_MODEL_NODES = 200000
_MAX_STRING_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_SUPPORTED_RANKINGS = frozenset(
    {
        "total", "total_stars", "stars",
        "daily", "24h", "growth_24h", "stars_24h",
        "weekly", "7d", "growth_7d", "stars_7d",
        "acceleration", "growth_acceleration",
    }
)


def _number(value: Any) -> str:
    """Faithfully format a finite model number without unbounded expansion."""

    if value is None:
        return "—"
    if type(value) not in {int, float, Decimal}:
        raise ValueError("report number must be a finite int, float, or Decimal")
    if type(value) is int:
        return format(value, ",d")
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError("report number must be finite")
    digits = decimal_value.as_tuple().digits
    adjusted = decimal_value.adjusted() if decimal_value else 0
    exponent = decimal_value.as_tuple().exponent
    if -12 <= adjusted <= 30 and len(digits) <= 128 and abs(exponent) <= 1000:
        fixed = format(decimal_value, "f")
        sign = ""
        if fixed.startswith("-"):
            sign, fixed = "-", fixed[1:]
        whole, dot, fraction = fixed.partition(".")
        result = sign + format(int(whole or "0"), ",d")
        if dot:
            result += "." + fraction
        return result
    sign = "-" if decimal_value.as_tuple().sign else ""
    significant_digits = list(digits[:32]) or [0]
    if len(digits) > 32 and digits[32] >= 5:
        cursor = len(significant_digits) - 1
        while cursor >= 0 and significant_digits[cursor] == 9:
            significant_digits[cursor] = 0
            cursor -= 1
        if cursor < 0:
            significant_digits = [1] + significant_digits[:-1]
            adjusted += 1
        else:
            significant_digits[cursor] += 1
    significant = "".join(str(digit) for digit in significant_digits)
    coefficient = significant[0]
    if len(significant) > 1:
        coefficient += "." + significant[1:]
    return "{}{}E{:+d}".format(sign, coefficient, adjusted)


def _signed_number(value: Any) -> str:
    rendered = _number(value)
    if type(value) in {int, float, Decimal} and value > 0:
        return "+" + rendered
    return rendered


def _fixed_number(value: Any, places: int = 2) -> str:
    """Format a finite model number with a fixed number of decimal places."""

    if type(value) not in {int, float, Decimal}:
        raise ValueError("report number must be a finite int, float, or Decimal")
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError("report number must be finite")
    return format(decimal_value, ",.{}f".format(places))


def _is_sensitive_key(key: Any) -> bool:
    if type(key) is not str:
        return False
    normalized = unicodedata.normalize("NFKC", key).strip()
    compact = re.sub(r"[^a-z0-9]", "", normalized.casefold())
    return bool(_SENSITIVE_KEY_PATTERN.search(normalized)) or any(
        marker in compact
        for marker in (
            "token", "authorization", "header", "cookie", "password",
            "passwd", "secret", "credential", "privatekey",
        )
    )


def _preflight_model(model: Any) -> None:
    """Iteratively bound and type-check a model before recursive validation."""

    stack = [(model, 0, False)]
    active = set()
    nodes = 0
    estimated_bytes = 0
    while stack:
        value, depth, exiting = stack.pop()
        value_type = type(value)
        if exiting:
            active.remove(id(value))
            continue

        nodes += 1
        if nodes > _MAX_MODEL_NODES:
            raise ValueError("report model exceeds the node limit")
        if value_type is str:
            size = len(value.encode("utf-8"))
            if size > _MAX_STRING_BYTES:
                raise ValueError("report model contains an oversized string")
            estimated_bytes += size + 2
        elif value_type in {type(None), bool}:
            estimated_bytes += 5
        elif value_type is int:
            estimated_bytes += max(1, int(value.bit_length() * 0.302) + 2)
        elif value_type is float:
            if not Decimal(str(value)).is_finite():
                raise ValueError("report model contains a non-finite number")
            estimated_bytes += 32
        elif value_type is Decimal:
            if not value.is_finite() or abs(value.adjusted() if value else 0) > 308:
                raise ValueError("report Decimal is outside the persistence range")
            estimated_bytes += len(value.as_tuple().digits) + 32
        elif value_type in {dict, list}:
            if depth > _MAX_MODEL_DEPTH:
                raise ValueError("report model exceeds the depth limit")
            identity = id(value)
            if identity in active:
                raise ValueError("report model contains a circular reference")
            active.add(identity)
            stack.append((value, depth, True))
            estimated_bytes += 2
            if value_type is dict:
                for key, item in value.items():
                    if type(key) is not str:
                        raise ValueError("report model keys must be plain text")
                    if _MODEL_KEY_PATTERN.fullmatch(key) is None:
                        raise ValueError("report model key must use safe ASCII characters")
                    if _is_sensitive_key(key):
                        raise ValueError("report model contains a sensitive key")
                    key_size = len(key.encode("ascii"))
                    if key_size > _MAX_STRING_BYTES:
                        raise ValueError("report model contains an oversized string")
                    nodes += 1
                    if nodes > _MAX_MODEL_NODES:
                        raise ValueError("report model exceeds the node limit")
                    estimated_bytes += key_size + 3
                    if key == "repository_type" and type(item) is not str:
                        raise ValueError("repository_type must be plain text")
                    stack.append((item, depth + 1, False))
            else:
                for item in reversed(value):
                    stack.append((item, depth + 1, False))
        else:
            raise ValueError("report model contains a non-JSON value")

        if estimated_bytes > _MAX_TOTAL_BYTES:
            raise ValueError("report model exceeds the size limit")


def _safe_text(value: Any) -> str:
    """Return inert, single-line Markdown table text."""

    if value is None:
        return "—"
    if type(value) is bool:
        text = "是" if value else "否"
    elif type(value) in {int, float, Decimal}:
        text = _number(value)
    elif type(value) is str:
        text = value
    else:
        raise ValueError("report text must use a plain JSON scalar")
    text = _CONTROL_PATTERN.sub("", text)
    text = _WHITESPACE_PATTERN.sub(" ", text)
    text = "".join(
        character
        for character in text
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )
    text = html.escape(text, quote=True)
    text = text.replace("\\", r"\\")
    for marker in ("`", "*", "_", "~", "[", "]", "(", ")", "|"):
        text = text.replace(marker, "\\" + marker)
    return text.strip() or "—"


def _plain_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if type(value) is list:
        return value
    raise ValueError("report list must be a plain array")


def _safe_mapping_items(value: Any) -> Iterable[Tuple[Any, Any]]:
    if type(value) is not dict:
        return ()
    return value.items()


def _repository_link(row: Mapping[str, Any]) -> str:
    raw_repo = row.get("repo", row.get("full_name", row.get("repository", "")))
    if type(raw_repo) is not str or _REPOSITORY_PATTERN.fullmatch(raw_repo) is None:
        return _safe_text(raw_repo)
    url = "https://github.com/{}".format(raw_repo)
    return "[{}]({})".format(_safe_text(raw_repo), html.escape(url, quote=True))


def _render_value(value: Any) -> str:
    if type(value) is dict:
        parts = []
        for key, item in _safe_mapping_items(value):
            parts.append("{}={}".format(_safe_text(key), _render_value(item)))
        return "；".join(parts) if parts else "—"
    if type(value) is list:
        parts = [_render_value(item) for item in value]
        return "；".join(part for part in parts if part != "—") or "—"
    return _safe_text(value)


def _field(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and not _is_sensitive_key(name):
            return row[name]
    return default


def _validate_tree(
    value: Any,
    allowed_decimals: Optional[set] = None,
    active: Optional[set] = None,
) -> None:
    if active is None:
        active = set()
    value_type = type(value)
    if value_type in {type(None), str, int, bool}:
        return
    if value_type is float:
        if not Decimal(str(value)).is_finite():
            raise ValueError("report model contains a non-finite number")
        return
    if value_type is Decimal:
        if not value.is_finite():
            raise ValueError("report model contains a non-finite number")
        if allowed_decimals is not None:
            remaining = allowed_decimals.get(id(value), 0)
            if remaining <= 0:
                raise ValueError("Decimal is allowed only in declared numeric fields")
            allowed_decimals[id(value)] = remaining - 1
        return
    if value_type is dict:
        identity = id(value)
        if identity in active:
            raise ValueError("report model contains a circular reference")
        active.add(identity)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("report model keys must be plain text")
                if _is_sensitive_key(key):
                    raise ValueError("report model contains a sensitive key")
                _validate_tree(item, allowed_decimals, active)
        finally:
            active.remove(identity)
        return
    if value_type is list:
        identity = id(value)
        if identity in active:
            raise ValueError("report model contains a circular reference")
        active.add(identity)
        try:
            for item in value:
                _validate_tree(item, allowed_decimals, active)
        finally:
            active.remove(identity)
        return
    raise ValueError("report model contains a non-JSON value")


def _iso_date(value: Any) -> str:
    if type(value) is not str:
        raise ValueError("metadata.date must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("metadata.date must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError("metadata.date must be an ISO date")
    return value


def _require_row(row: Any, section: str) -> Dict[str, Any]:
    if type(row) is not dict:
        raise ValueError("{} entries must be plain mappings".format(section))
    return row


def _require_present(row: Mapping[str, Any], aliases: Sequence[str], field: str) -> Any:
    for alias in aliases:
        if alias in row:
            return row[alias]
    raise ValueError("{} is required".format(field))


def _require_text(row: Mapping[str, Any], aliases: Sequence[str], field: str) -> str:
    value = _require_present(row, aliases, field)
    if type(value) is not str:
        raise ValueError("{} must be text".format(field))
    return value


def _canonical_relation(value: Any) -> str:
    """Reject invisible semantic changes, then normalize outer whitespace."""

    if type(value) is not str:
        raise ValueError("replacement relation must be plain text")
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        for character in value
    ):
        raise ValueError("replacement relation contains a control character")
    return value.strip()


def _require_number(
    row: Mapping[str, Any],
    aliases: Sequence[str],
    field: str,
    nullable: bool = False,
    allowed_decimals: Optional[set] = None,
) -> Any:
    value = _require_present(row, aliases, field)
    if value is None and nullable:
        return value
    _number(value)
    if type(value) is Decimal and allowed_decimals is not None:
        allowed_decimals[id(value)] = allowed_decimals.get(id(value), 0) + 1
    return value


def _require_list(row: Mapping[str, Any], aliases: Sequence[str], field: str) -> list:
    value = _require_present(row, aliases, field)
    if type(value) is not list:
        raise ValueError("{} must be an array".format(field))
    return value


def _require_text_list(
    row: Mapping[str, Any], aliases: Sequence[str], field: str
) -> list:
    values = _require_list(row, aliases, field)
    if any(type(value) is not str for value in values):
        raise ValueError("{} entries must be plain text".format(field))
    return values


def _validate_optional_text(
    row: Mapping[str, Any], aliases: Sequence[str], field: str
) -> None:
    for alias in aliases:
        if alias in row:
            if type(row[alias]) is not str:
                raise ValueError("{} must be plain text".format(field))
            return


def _validate_evidence(value: list, field: str) -> None:
    for item in value:
        if type(item) is not dict:
            raise ValueError("{} entries must be plain mappings".format(field))


def _validate_report_model(model: Any) -> Tuple[Mapping[str, Any], Mapping[str, Any], str]:
    if type(model) is not dict or not model:
        raise ValueError("report model must be a non-empty mapping")
    # First pass rejects behavior-rewriting subclasses and non-JSON containers
    # before any field access, formatting, or string normalization occurs.
    _preflight_model(model)
    _validate_tree(model)
    allowed_decimals = {}
    metadata = model.get("metadata")
    if type(metadata) is not dict:
        raise ValueError("report metadata must be a mapping")
    report_date = _iso_date(metadata.get("date"))
    _validate_optional_text(metadata, ("query_time",), "metadata query_time")
    periods = metadata.get("periods")
    if periods is not None:
        if type(periods) is not dict or any(
            type(value) is not str for value in periods.values()
        ):
            raise ValueError("metadata periods must map to plain text")
    ranking_sources = metadata.get("ranking_sources")
    if ranking_sources is not None:
        if type(ranking_sources) is not dict or any(
            type(value) is not str for value in ranking_sources.values()
        ):
            raise ValueError("ranking_sources must map to plain text")

    rankings = model.get("rankings")
    if type(rankings) is not dict:
        raise ValueError("rankings must be a mapping")
    unknown = [key for key in rankings if key not in _SUPPORTED_RANKINGS]
    if unknown:
        raise ValueError("rankings contains an unsupported ranking type")
    for ranking_name, rows in rankings.items():
        if type(rows) is not list:
            raise ValueError("each ranking must be an array")
        if ranking_name in {"total", "total_stars", "stars"}:
            metric_names = ("value", "total_stars", "stars")
        elif ranking_name in {"daily", "24h", "growth_24h", "stars_24h"}:
            metric_names = ("growth", "value", "stars_24h_external", "stars_24h_local")
        elif ranking_name in {"weekly", "7d", "growth_7d", "stars_7d"}:
            metric_names = ("growth", "value", "stars_7d_external", "stars_7d_local")
        else:
            metric_names = ("value", "growth_acceleration", "acceleration")
        for row in rows:
            row = _require_row(row, "ranking")
            _require_text(row, ("repo", "full_name", "repository"), "ranking repository")
            _require_number(
                row, metric_names, "ranking metric",
                allowed_decimals=allowed_decimals,
            )
            _validate_optional_text(
                row, ("source", "data_source"), "ranking source"
            )
            for field in (
                "description", "primary_category", "repository_type",
                "purpose", "problem_solved", "data_scope", "metric_name", "unit",
            ):
                _validate_optional_text(row, (field,), "ranking {}".format(field))
            if ranking_name in {"total", "total_stars", "stars"}:
                _require_text(row, ("repository_type", "type"), "repository type")

    for key in (
        "category_trends", "reusable_projects", "featured_projects",
        "history", "market_trends", "watchlist",
    ):
        if key in model and type(model[key]) is not list:
            raise ValueError("report section arrays must be arrays")

    overview = model.get("overview", {})
    if type(overview) is not dict:
        raise ValueError("overview must be a mapping")
    for field in ("facts", "inferences", "actions"):
        if field in overview:
            _require_text_list(overview, (field,), "overview {}".format(field))

    for item in model.get("category_trends", []):
        row = _require_row(item, "category trend")
        _require_text(row, ("category", "name"), "category")
        _require_text_list(row, ("facts",), "category facts")
        _require_number(
            row, ("change", "previous_change"), "category change",
            nullable=True, allowed_decimals=allowed_decimals,
        )
        _require_text(row, ("confidence",), "category confidence")

    for item in model.get("reusable_projects", []):
        row = _require_row(item, "reusable project")
        repository_type = row.get("repository_type", row.get("type", ""))
        if type(repository_type) is not str:
            raise ValueError("repository_type must be plain text")
        _require_text(row, ("repo", "full_name", "repository"), "reusable repository")
        if "purpose" in row:
            _require_text(row, ("purpose",), "reusable purpose")
        if "problem_solved" in row:
            _require_text(row, ("problem_solved",), "reusable problem")
        if "description_source" in row:
            _require_text(row, ("description_source",), "description source")
        if "total_stars" in row:
            _require_number(
                row, ("total_stars",), "reusable total stars",
                nullable=True, allowed_decimals=allowed_decimals,
            )
        _require_text(row, ("reuse", "reusable_content"), "reusable content")
        _require_text(row, ("integration", "integration_form"), "integration form")
        _require_number(
            row, ("score",), "reusability score",
            allowed_decimals=allowed_decimals,
        )
        _require_text(row, ("license",), "license")
        _require_text(row, ("maintenance",), "maintenance")
        _require_text_list(row, ("risks", "risk"), "risks")
        _require_text_list(row, ("actions", "action"), "actions")
        if "runnable" in row and type(row["runnable"]) is not bool:
            raise ValueError("runnable must be boolean")

    for item in model.get("featured_projects", []):
        row = _require_row(item, "featured project")
        _require_text(row, ("repo", "full_name", "repository"), "featured repository")
        _require_text_list(row, ("facts",), "featured facts")
        _require_text_list(row, ("inferences",), "featured inferences")
        evidence = _require_list(row, ("evidence",), "featured evidence")
        _validate_evidence(evidence, "featured evidence")
        _require_text(row, ("confidence",), "featured confidence")
        _require_text_list(row, ("actions", "action"), "featured actions")

    for item in model.get("history", []):
        row = _require_row(item, "history")
        _require_text(row, ("repo", "full_name", "repository"), "history repository")
        _require_text_list(row, ("facts",), "history facts")
        _require_text_list(row, ("inferences", "inferred_reasons"), "history inferences")
        evidence = _require_list(row, ("evidence",), "history evidence")
        _validate_evidence(evidence, "history evidence")
        _require_text(row, ("confidence",), "history confidence")
        relation = _canonical_relation(
            _require_text(
                row,
                ("replacement", "replacement_relation"),
                "replacement relation",
            )
        )
        if relation == "直接替代" and not any(
            type(evidence_item) is dict
            and type(evidence_item.get("type")) is str
            and evidence_item.get("type") in DIRECT_EVIDENCE
            for evidence_item in evidence
        ):
            raise ValueError("direct replacement requires canonical evidence")
        _require_text_list(row, ("actions", "action"), "history actions")

    for item in model.get("market_trends", []):
        row = _require_row(item, "market trend")
        _require_text(row, ("conclusion",), "market conclusion")
        evidence = _require_list(row, ("evidence",), "market evidence")
        _validate_evidence(evidence, "market evidence")
        _require_number(
            row, ("change", "previous_change"), "market change",
            nullable=True, allowed_decimals=allowed_decimals,
        )
        _require_text(row, ("confidence",), "market confidence")
        _require_number(
            row, ("consecutive_periods", "periods"), "consecutive periods",
            allowed_decimals=allowed_decimals,
        )

    for item in model.get("watchlist", []):
        row = _require_row(item, "watchlist")
        _require_text(row, ("repo", "full_name", "repository"), "watchlist repository")
        _require_text(row, ("reason",), "watchlist reason")
        _require_text_list(row, ("actions", "action"), "watchlist actions")

    quality = model.get("data_quality", {})
    if type(quality) is not dict:
        raise ValueError("data_quality must be a mapping")
    for owner, field in ((metadata, "source_status"), (quality, "source_status")):
        if field in owner:
            status_rows = owner[field]
            if type(status_rows) is not list:
                raise ValueError("source_status must be an array")
            for status in status_rows:
                status = _require_row(status, "source status")
                _validate_optional_text(status, ("source",), "source status source")
                _validate_optional_text(status, ("status",), "source status value")
    _validate_optional_text(quality, ("query_time",), "data quality query_time")
    quality_periods = quality.get("periods")
    if quality_periods is not None:
        if type(quality_periods) is list:
            if any(type(value) is not str for value in quality_periods):
                raise ValueError("data quality periods must be plain text")
        elif type(quality_periods) is dict:
            if any(type(value) is not str for value in quality_periods.values()):
                raise ValueError("data quality periods must map to plain text")
        else:
            raise ValueError("data quality periods must be a list or mapping")
    for field in ("warnings", "missing", "conflicts", "manual_inferences"):
        if field in quality:
            _require_text_list(quality, (field,), "data quality {}".format(field))
    remaining_decimals = dict(allowed_decimals)
    _validate_tree(model, remaining_decimals)
    if any(remaining_decimals.values()):
        raise ValueError("declared Decimal field was not validated")
    return metadata, rankings, report_date


def _paragraph_items(value: Any) -> str:
    items = _plain_list(value)
    if not items:
        return "- 暂无可用数据（数据降级）\n"
    return "".join("- {}\n".format(_render_value(item)) for item in items)


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return "暂无可用数据（数据降级）\n"
    output = [
        "| {} |".format(" | ".join(headers)),
        "| {} |".format(" | ".join("---" for _ in headers)),
    ]
    output.extend("| {} |".format(" | ".join(row)) for row in rows)
    return "\n".join(output) + "\n"


def _ranking_rows(rows: Sequence[Mapping[str, Any]], kind: str) -> List[List[str]]:
    rendered = []
    for index, row in enumerate(rows, 1):
        if kind == "total":
            value = _field(row, "value", "total_stars", "stars")
            metric = _number(value)
        else:
            names = {
                "daily": ("growth", "value", "stars_24h_external", "stars_24h_local"),
                "weekly": ("growth", "value", "stars_7d_external", "stars_7d_local"),
                "acceleration": ("value", "growth_acceleration", "acceleration"),
            }[kind]
            value = _field(row, *names)
            if kind in {"daily", "weekly"}:
                metric = _signed_number(value)
            else:
                metric = _fixed_number(value, 2)
        rendered_row = [
            _number(index),
            _repository_link(row),
            _render_value(_field(row, "primary_category", default="未分类")),
            _render_value(_field(row, "repository_type", "type", default="未识别")),
            _render_value(_field(row, "purpose", "description", default="仓库简介不足，需进一步核验。")),
            _render_value(_field(row, "problem_solved", default="仓库简介不足，需进一步核验。")),
        ]
        if kind in {"daily", "weekly"}:
            total_stars = _field(row, "total_stars", "current_total_stars")
            rendered_row.append(_number(total_stars) if total_stars is not None else "缺失")
        rendered_row.extend([
            metric,
            _render_value(_field(row, "data_scope", "source", "data_source", default="模型未提供")),
        ])
        rendered.append(rendered_row)
    return rendered


def _ranking_block(
    title: str,
    kind: str,
    rows: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any],
) -> str:
    periods = metadata.get("periods", {})
    period = None
    if isinstance(periods, Mapping):
        aliases = {
            "total": ("total", "total_stars"),
            "daily": ("daily", "24h", "growth_24h"),
            "weekly": ("weekly", "7d", "growth_7d"),
            "acceleration": ("acceleration",),
        }[kind]
        period = _field(periods, *aliases)
    sources = metadata.get("ranking_sources", {})
    source = None
    if isinstance(sources, Mapping):
        source = _field(sources, kind)
    if source is None:
        row_sources = [
            _field(row, "source", "data_source")
            for row in rows
            if _field(row, "source", "data_source") is not None
        ]
        source = row_sources if row_sources else "模型未提供"
    block = [
        "## {}".format(title),
        "",
        "- 统计周期：{}".format(_render_value(period)),
        "- 数据源：{}".format(_render_value(source)),
        "",
    ]
    metric_headers = {
        "total": "当前累计 Star（个）",
        "daily": "过去24小时新增 Star（个）",
        "weekly": "过去7天新增 Star（个）",
        "acceleration": "24h增速相对7日日均（倍）",
    }
    headers = [
        "序号", "仓库", "一级分类", "仓库类型", "用途与定位",
        "主要解决问题",
    ]
    if kind in {"daily", "weekly"}:
        headers.append("当前累计 Star（个）")
    headers.extend([metric_headers[kind], "数据口径"])
    if kind in {"daily", "weekly"}:
        block.extend([
            "- 指标说明：`+N` 表示该统计窗口内新增约 N 个 Star，不是累计 Star、评分或排名。",
            "",
        ])
    elif kind == "acceleration":
        block.extend([
            "- 指标说明：最近24小时新增 ÷ 最近7天平均每日新增；大于 1 表示最近一天高于周均速度。",
            "",
        ])
    block.append(_table(headers, _ranking_rows(rows, kind)).rstrip())
    return "\n".join(block)


def _get_ranking(rankings: Mapping[str, Any], aliases: Sequence[str]) -> List[Mapping[str, Any]]:
    for alias in aliases:
        if alias in rankings:
            return rankings[alias]
    return []


def _structured_row(row: Mapping[str, Any], fields: Sequence[Tuple[str, Sequence[str]]]) -> List[str]:
    result = []
    for _, aliases in fields:
        result.append(_render_value(_field(row, *aliases)))
    return result


def build_report(model: Mapping[str, Any]) -> str:
    """Validate and deterministically render one canonical report model.

    Rankings and all metrics are consumed in their supplied order.  The
    renderer never sorts repositories or derives growth/ranking values.
    """

    metadata, rankings, report_date = _validate_report_model(model)
    output = ["# {}（{}）".format(REPORT_TITLE, report_date), ""]

    output.extend(["# {}".format(SECTION_TITLES[0]), ""])
    overview = model.get("overview", {})
    if not isinstance(overview, Mapping):
        raise ValueError("overview must be a mapping")
    output.extend(["## 事实", "", _paragraph_items(overview.get("facts")).rstrip(), ""])
    output.extend(["## 推断", "", _paragraph_items(overview.get("inferences")).rstrip(), ""])
    output.extend(["## 行动", "", _paragraph_items(overview.get("actions")).rstrip(), ""])

    output.extend(["# {}".format(SECTION_TITLES[1]), ""])
    ranking_specs = (
        ("总 Star 排行榜", "total", ("total", "total_stars", "stars")),
        ("24h 新增排行榜", "daily", ("daily", "24h", "growth_24h", "stars_24h")),
        ("7 日新增排行榜", "weekly", ("weekly", "7d", "growth_7d", "stars_7d")),
    )
    for title, kind, aliases in ranking_specs:
        output.extend([_ranking_block(title, kind, _get_ranking(rankings, aliases), metadata), ""])
    acceleration = _get_ranking(rankings, ("acceleration", "growth_acceleration"))
    if acceleration:
        output.extend([_ranking_block("增长加速度", "acceleration", acceleration, metadata), ""])

    output.extend(["# {}".format(SECTION_TITLES[2]), ""])
    category_fields = (
        ("分类", ("category", "name")),
        ("事实", ("facts",)),
        ("上期变化", ("change", "previous_change")),
        ("可信度", ("confidence",)),
    )
    category_rows = [
        _structured_row(row, category_fields)
        for row in model.get("category_trends", [])
    ]
    output.extend([_table([item[0] for item in category_fields], category_rows).rstrip(), ""])

    output.extend(["# {}".format(SECTION_TITLES[3]), ""])
    output.extend([
        "- 评分说明：满分 100 分；许可证 20、维护 20、文档 15、Release 15、集成 15、社区 10、CI 5。缺失信号按 0 分计，评分仅作为复用筛选线索，不代表可直接采用。",
        "- 维护说明：按最近推送时间判断；90 天内为“活跃”，91–365 天为“需复核”，超过 365 天为“低活跃”；已归档仓库标为“已归档”，缺少有效推送时间标为“信息不足”。",
        "",
    ])
    reusable_fields = (
        ("仓库", ("repo",)),
        ("用途与定位", ("purpose",)),
        ("主要解决问题", ("problem_solved",)),
        ("描述来源", ("description_source",)),
        ("当前累计 Star（个）", ("total_stars",)),
        ("可复用内容", ("reuse", "reusable_content")),
        ("接入形式", ("integration", "integration_form")),
        ("评分", ("score",)),
        ("信号覆盖", ("score_confidence",)),
        ("许可证", ("license",)),
        ("维护", ("maintenance",)),
        ("风险", ("risks", "risk")),
        ("动作", ("actions", "action")),
    )
    reusable_rows = []
    for row in model.get("reusable_projects", []):
        repository_type = _field(row, "repository_type", "type", default="")
        if type(repository_type) is not str:
            raise ValueError("repository_type must be plain text")
        normalized_type = repository_type.casefold()
        information_only = any(
            marker in normalized_type
            for marker in (
                "信息", "资讯", "资料", "教程", "聚合", "榜单",
                "awesome", "information_repository", "resource list",
            )
        )
        if row.get("runnable") is False or information_only:
            continue
        cells = [_repository_link(row)]
        cells.extend(
            _structured_row(row, reusable_fields[1:])
        )
        reusable_rows.append(cells)
    output.extend([_table([item[0] for item in reusable_fields], reusable_rows).rstrip(), ""])

    output.extend(["# {}".format(SECTION_TITLES[4]), ""])
    featured = model.get("featured_projects", [])[:5]
    if not featured:
        output.extend(["暂无可用数据（数据降级）", ""])
    else:
        for index, row in enumerate(featured, 1):
            output.extend(
                [
                    "## {}. {}".format(index, _repository_link(row)),
                    "",
                    "- 事实：{}".format(_render_value(_field(row, "facts"))),
                    "- 推断：{}".format(_render_value(_field(row, "inferences"))),
                    "- 证据：{}".format(_render_value(_field(row, "evidence"))),
                    "- 可信度：{}".format(_render_value(_field(row, "confidence"))),
                    "- 行动建议：{}".format(_render_value(_field(row, "actions", "action"))),
                    "",
                ]
            )

    output.extend(["# {}".format(SECTION_TITLES[5]), "", "> 退榜不等于过时；只陈述模型中已记录的替代证据。", ""])
    history_fields = (
        ("仓库", ("repo",)),
        ("事实", ("facts",)),
        ("推断原因", ("inferences", "inferred_reasons")),
        ("证据", ("evidence",)),
        ("可信度", ("confidence",)),
        ("替代关系", ("replacement", "replacement_relation")),
        ("行动建议", ("actions", "action")),
    )
    history_rows = []
    for row in model.get("history", []):
        evidence = _field(row, "evidence")
        replacement = _canonical_relation(
            _field(row, "replacement", "replacement_relation")
        )
        cells = [
            _repository_link(row),
            _render_value(_field(row, "facts")),
            _render_value(_field(row, "inferences", "inferred_reasons")),
            _render_value(evidence),
            _render_value(_field(row, "confidence")),
            _render_value(replacement),
            _render_value(_field(row, "actions", "action")),
        ]
        history_rows.append(cells)
    output.extend([_table([item[0] for item in history_fields], history_rows).rstrip(), ""])

    output.extend(["# {}".format(SECTION_TITLES[6]), ""])
    market_fields = (
        ("结论", ("conclusion",)),
        ("具体数据证据", ("evidence",)),
        ("上期变化", ("change", "previous_change")),
        ("可信度", ("confidence",)),
        ("连续周期", ("consecutive_periods", "periods")),
    )
    market_rows = [
        _structured_row(row, market_fields)
        for row in model.get("market_trends", [])
    ]
    output.extend([_table([item[0] for item in market_fields], market_rows).rstrip(), ""])

    output.extend(["# {}".format(SECTION_TITLES[7]), ""])
    watch_fields = (
        ("仓库", ("repo",)),
        ("观察理由", ("reason", "facts")),
        ("下一步动作", ("actions", "action")),
    )
    watch_rows = []
    for row in model.get("watchlist", []):
        watch_rows.append([_repository_link(row)] + _structured_row(row, watch_fields[1:]))
    output.extend([_table([item[0] for item in watch_fields], watch_rows).rstrip(), ""])

    output.extend(["# {}".format(SECTION_TITLES[8]), ""])
    quality = model.get("data_quality", {})
    if not isinstance(quality, Mapping):
        raise ValueError("data_quality must be a mapping")
    quality_rows = [
        ("来源状态", quality.get("source_status", metadata.get("source_status"))),
        ("查询时间", quality.get("query_time", metadata.get("query_time"))),
        ("查询周期与 external/local 口径", quality.get("periods", metadata.get("periods"))),
        ("缺失", quality.get("missing")),
        ("降级与警告", quality.get("warnings")),
        ("冲突", quality.get("conflicts")),
        ("人工推断", quality.get("manual_inferences")),
    ]
    visible_quality = [
        [_safe_text(label), _render_value(value)]
        for label, value in quality_rows
        if value not in (None, [], {})
    ]
    output.append(_table(["项目", "说明"], visible_quality).rstrip())

    return "\n".join(output).rstrip() + "\n"


__all__ = [
    "REPORT_TITLE", "SECTION_TITLES", "_number", "_validate_report_model",
    "build_report",
]
