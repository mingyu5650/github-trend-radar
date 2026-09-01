"""Command-line orchestration for the GitHub trend radar."""

import argparse
import copy
import json
import math
import os
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from classify import classify_repository, merge_records
from config import (
    DEFAULT_CATEGORY_RULES,
    FIXED_PRIMARY_CATEGORIES,
    RadarPaths,
    ensure_default_category_rules,
    validate_fixed_primary_categories,
)
from fetch_github import (
    fetch_readme_sections, fetch_repo_details, fetch_repo_star_count,
    fetch_top_repositories,
)
from fetch_ossinsight import fetch_trend
from fetch_trending import fetch_trending
from history import calculate_growth_acceleration, calculate_local_growth, detect_cooling, load_history_rows, upsert_history_rows
from html_report import build_html_report
from http_client import SourceError
from models import RepositoryRecord
from replacement import analyze_replacement
from report import build_report
from score import SIGNAL_NAMES, score_reusability
from storage import exclusive_file_lock, save_complete_report
from watchlist import read_watchlist, update_automatic_fields


OUTPUT_DIRECTORY = Path("GitHub开源趋势雷达")
BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
OPTIONAL_NUMERIC_FIELDS = (
    "total_stars",
    "stars_24h_external",
    "stars_7d_external",
    "stars_30d_external",
    "stars_24h_local",
    "stars_7d_local",
    "growth_acceleration",
    "forks",
    "open_issues",
)


class SafeArgumentParser(argparse.ArgumentParser):
    """An argument parser whose diagnostics never echo untrusted values."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "error: invalid command line input\n")


def _top_value(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("top must be an integer from 1 to 100")
    if not 1 <= result <= 100:
        raise argparse.ArgumentTypeError("top must be an integer from 1 to 100")
    return result


def _category_value(value):
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("category must be safe text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > 80
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise argparse.ArgumentTypeError("category must be safe text")
    if normalized not in FIXED_PRIMARY_CATEGORIES:
        raise argparse.ArgumentTypeError("category must be a fixed primary category")
    return normalized


def _repository_value(value):
    try:
        return RepositoryRecord(full_name=value).full_name
    except (TypeError, ValueError, AttributeError):
        raise argparse.ArgumentTypeError("repository must use owner/repo format")


def _workspace_value(value):
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("workspace must be a safe path")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or any(
            unicodedata.category(character).startswith("C")
            for character in normalized
        )
    ):
        raise argparse.ArgumentTypeError("workspace must be a safe path")
    return Path(normalized).expanduser()


def _validate_record_numbers(record):
    if not isinstance(record, RepositoryRecord):
        raise ValueError("repository output must use RepositoryRecord")
    for field_name in OPTIONAL_NUMERIC_FIELDS:
        value = getattr(record, field_name)
        if value is None:
            continue
        if type(value) not in {int, float}:
            raise ValueError("repository numeric fields must be finite numbers")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("repository numeric fields must be finite numbers")


def _strict_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_parser():
    parser = SafeArgumentParser(prog="radar.py")
    parser.add_argument("--workspace", type=_workspace_value)
    parser.add_argument(
        "--output-root",
        type=_workspace_value,
        help="direct radar asset directory; does not add the default subdirectory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    report = subparsers.add_parser("report")
    report.add_argument("--category", type=_category_value)
    report.add_argument("--save", action="store_true")
    report.add_argument("--top", type=_top_value, default=20)
    repo = subparsers.add_parser("repo")
    repo.add_argument("full_name", type=_repository_value)
    compare = subparsers.add_parser("compare")
    compare.add_argument("old", type=_repository_value)
    compare.add_argument("new", type=_repository_value)
    subparsers.add_parser("watchlist")
    return parser


def should_save(args):
    return args.command == "report" and (args.category is None or bool(args.save))


def _is_current_run_latest_artifact(name: str, run_date: str) -> bool:
    """Return True when a 最新报告 file belongs to the successful run date."""
    return name.endswith(
        (
            "-{}.md".format(run_date),
            "-{}.json".format(run_date),
            "-{}.html".format(run_date),
        )
    )


def cleanup_stale_latest_reports(latest_dir, run_date: str) -> None:
    """Keep only the successful run-date artifacts under 最新报告/.

    After a full report is archived and written to 最新报告/, older dated
    complete/category files and any stable *-最新.* aliases are removed so the
    directory does not accumulate duplicate daily copies.
    """
    directory = Path(latest_dir)
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if _is_current_run_latest_artifact(path.name, run_date):
            continue
        path.unlink()


def _report_targets(paths, category=None):
    if category is None:
        return (
            paths.archive_report,
            paths.archive_data,
            paths.latest_report,
            paths.latest_data,
        )
    if (
        category not in FIXED_PRIMARY_CATEGORIES
        or any(character in category for character in "/\\")
        or any(
            unicodedata.category(character).startswith("C")
            for character in category
        )
    ):
        raise ValueError("category must be safe for report paths")
    report_stem = "GitHub开源趋势与项目复用雷达-分类-{}".format(category)
    data_stem = "原始数据-分类-{}".format(category)
    return (
        paths.archive_dir / "{}-{}.md".format(report_stem, paths.run_date),
        paths.archive_dir / "{}-{}.json".format(data_stem, paths.run_date),
        paths.latest_report.parent / "{}-{}.md".format(report_stem, paths.run_date),
        paths.latest_data.parent / "{}-{}.json".format(data_stem, paths.run_date),
    )


@dataclass
class RadarServices:
    fetch_top: Callable = fetch_top_repositories
    fetch_details: Callable = fetch_repo_details
    fetch_star_count: Callable = fetch_repo_star_count
    fetch_readme: Callable = fetch_readme_sections
    fetch_trend: Callable = fetch_trend
    fetch_trending: Callable = fetch_trending
    save_report: Callable = save_complete_report
    load_history: Callable = load_history_rows
    upsert_history: Callable = upsert_history_rows
    read_watchlist: Callable = read_watchlist
    update_watchlist: Callable = update_automatic_fields


def _safe_error(message):
    print(message, file=sys.stderr)


def _has_source(record, source, scope=None, period=None):
    return any(
        isinstance(item, Mapping)
        and item.get("source") == source
        and (scope is None or item.get("scope") == scope)
        and (period is None or item.get("period") == period)
        for item in record.source_records
    )


def _age_days(value, now):
    if not value:
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds() / 86400)
    except (TypeError, ValueError, OverflowError):
        return None


def _reusability(record, now):
    signals = {}
    detail_collected = _has_source(record, "github", "repository_detail")
    age = _age_days(record.pushed_at, now)
    if detail_collected and age is not None:
        signals["maintenance"] = 1.0 if age <= 90 else 0.5 if age <= 365 else 0.0
    release_statuses = {
        item.get("release")
        for item in record.source_records
        if isinstance(item, Mapping)
        and item.get("source") == "github"
        and item.get("scope") == "repository_detail"
        and item.get("release")
    } | {
        item.get("status")
        for item in record.source_records
        if isinstance(item, Mapping)
        and item.get("source") == "github_latest_release"
        and item.get("status")
    }
    if detail_collected and (
        record.latest_release or "available" in release_statuses
    ):
        signals["releases"] = 1.0
    elif detail_collected and "release_fetch_failed" not in release_statuses:
        # A successfully collected repository detail with no release is a
        # known zero.  Keep the signal missing only when the separate release
        # lookup explicitly failed.
        signals["releases"] = 0.0
    if (
        detail_collected
        and record.total_stars is not None
        and record.forks is not None
        and record.forks >= 0
    ):
        signals["community"] = (
            min(1.0, record.forks / record.total_stars * 10.0)
            if record.total_stars > 0 else 0.0
        )
    desc = (record.description or "").strip()
    if len(desc) >= 200:
        signals["docs"] = 1.0
    elif len(desc) >= 80:
        signals["docs"] = 0.67
    elif len(desc) >= 30:
        signals["docs"] = 0.33
    elif len(desc) > 0:
        signals["docs"] = 0.1
    topics = record.topics or []
    if len(topics) >= 5:
        signals["docs"] = max(signals.get("docs", 0), 0.8)
    elif len(topics) >= 2:
        signals["docs"] = max(signals.get("docs", 0), 0.5)
    repo_type = (record.repository_type or "").casefold()
    lang = (record.primary_language or "").casefold()
    integration_indicators = {"sdk", "库", "library", "kit", "framework", "plugin", "extension"}
    lang_indicators = {"python", "javascript", "typescript", "go", "rust", "java", "c#", "c++", "swift", "kotlin"}
    if any(ind in repo_type for ind in integration_indicators):
        signals["integration"] = 1.0
    elif any(ind in lang for ind in lang_indicators):
        signals["integration"] = 0.67
    elif any(ind in repo_type for ind in {"可运行软件", "cli", "tool"}):
        signals["integration"] = 0.33
    ci_indicators = {"ci-cd", "continuous-integration", "github-actions", "ci"}
    if any(ind in [t.casefold() for t in topics] for ind in ci_indicators):
        signals["ci"] = 1.0
    result = score_reusability(record, signals)
    result["signals"] = signals
    result["missing_signals"] = sorted(set(SIGNAL_NAMES) - set(signals))
    coverage = len(signals) / len(SIGNAL_NAMES)
    result["confidence"] = "高" if coverage >= 1.0 else "中" if coverage >= 0.5 else "低"
    return result


def _maintenance(record, now):
    if record.archived:
        return "已归档"
    age = _age_days(record.pushed_at, now)
    if age is None:
        return "信息不足"
    return "活跃" if age <= 90 else "需复核" if age <= 365 else "低活跃"


_PROBLEM_BY_TYPE = {
    "Awesome 清单": "降低相关工具、资料和方案的发现与筛选成本。",
    "教程或课程": "降低相关主题的学习、实践与上手门槛。",
    "书籍或资料": "系统整理相关知识，降低检索与学习成本。",
    "数据集": "提供可复用数据，减少数据采集、清洗和整理工作。",
    "模型": "提供可评估或集成的模型能力，减少从零训练与实现成本。",
    "模板": "提供可复用项目骨架，减少从零搭建和重复配置工作。",
    "SDK 或库": "提供可集成的基础能力，减少重复开发与维护成本。",
}
_PROBLEM_BY_CATEGORY = {
    "AI": "解决 AI 应用、智能体或模型能力落地中的具体问题。",
    "开发工具": "提升开发、调试、代码理解或自动化效率。",
    "基础设施与 DevOps": "降低部署、交付、运维或基础设施管理成本。",
    "数据与数据库": "解决数据存储、处理、分析或数据工程效率问题。",
    "安全": "降低安全检测、防护、权限或隐私风险。",
    "前端与 UI": "提升界面开发、设计系统或 Web 交互实现效率。",
    "移动端": "提升移动应用开发、运行或跨平台交付效率。",
}


def _repository_purpose(record, override=None):
    category = record.primary_category or "未分类"
    repository_type = record.repository_type or "开源项目"
    if isinstance(override, Mapping) and override.get("用途与定位"):
        return override["用途与定位"]
    description = record.description if isinstance(record.description, str) else ""
    description = description.strip()
    if description and _has_chinese(description):
        return f"{category}领域的{repository_type}。官方简介：{description}"
    return f"定位为{category}领域的{repository_type}，具体能力边界需结合项目文档核验。"


def _problem_solved(record, override=None):
    if isinstance(override, Mapping) and override.get("主要解决问题"):
        return override["主要解决问题"]
    description = record.description if isinstance(record.description, str) else ""
    description = description.strip()
    if record.repository_type in _PROBLEM_BY_TYPE:
        problem = _PROBLEM_BY_TYPE[record.repository_type]
    else:
        problem = _PROBLEM_BY_CATEGORY.get(
            record.primary_category,
            "针对仓库简介所述场景提供工具或解决方案，具体边界需进一步核验。",
        )
    if description and _has_chinese(description):
        context = description if len(description) <= 240 else description[:237] + "..."
        return f"{problem} 官方简介聚焦：{context}"
    return problem


def _reusable_content(record):
    """Describe reusable material in Chinese without translating unverified claims."""
    category = record.primary_category or "未分类"
    repository_type = record.repository_type or "开源项目"
    tags = [
        value.strip()
        for value in record.secondary_tags
        if isinstance(value, str) and value.strip()
    ]
    if tags:
        return "{}领域的{}，可用于{}相关能力的评估与集成。".format(
            category, repository_type, "、".join(tags[:3])
        )
    return "{}领域的{}及其源码，可用于相关能力的评估与集成。".format(
        category, repository_type
    )


def _reusable_purpose(record):
    category = record.primary_category or "未分类"
    repository_type = record.repository_type or "开源项目"
    tags = [
        value.strip()
        for value in record.secondary_tags
        if isinstance(value, str) and value.strip()
    ]
    if tags:
        return "定位为{}领域的{}，重点面向{}。".format(
            category, repository_type, "、".join(tags[:3])
        )
    return "定位为{}领域的{}，具体能力边界需结合项目文档核验。".format(
        category, repository_type
    )


def _reusable_problem(record):
    return _PROBLEM_BY_TYPE.get(
        record.repository_type,
        _PROBLEM_BY_CATEGORY.get(
            record.primary_category,
            "针对项目所述场景提供可运行能力，具体解决范围需进一步核验。",
        ),
    )


def _has_chinese(text):
    return isinstance(text, str) and any("\u4e00" <= char <= "\u9fff" for char in text)


def _data_scope(source):
    if source == "GitHub total stars":
        return "GitHub 当前累计值"
    if source == "OSSInsight 24h external":
        return "OSSInsight 外部统计（24h）"
    if source == "OSSInsight 7d external":
        return "OSSInsight 外部统计（7d）"
    if source == "local history 24h":
        return "本地历史快照计算（24h）"
    if source == "local history 7d":
        return "本地历史快照计算（7d）"
    if source == "local history":
        return "本地历史快照计算"
    return source


def _watch_update(record, now):
    maintenance = _maintenance(record, now)
    action = (
        "评估迁移或停止采用" if record.archived
        else "评估最新版本并安排升级" if record.latest_release
        else "继续观察" if maintenance == "活跃"
        else "人工复核维护状态"
    )
    return {
        "最近检查时间": now.date().isoformat(),
        "最新版本": record.latest_release or "未获取",
        "维护状态": maintenance,
        "建议动作": action,
    }


class RadarApp:
    def __init__(self, workspace=None, services=None, today=None, now=None):
        self.workspace = (
            Path(workspace)
            if workspace is not None
            else Path(__file__).resolve().parents[4] / OUTPUT_DIRECTORY
        )
        self.services = services or RadarServices()
        # Kept only for constructor compatibility. A run never mixes this
        # date source with its clock-derived metadata and history timestamps.
        self.today = today or date.today
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _run_now(self):
        value = self.now()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError("clock must return an aware datetime")
        return value.astimezone(BUSINESS_TIMEZONE)

    def _classified_detail(self, full_name):
        validated = RepositoryRecord(full_name=full_name).full_name
        record = copy.deepcopy(self.services.fetch_details(validated))
        return classify_repository(record, copy.deepcopy(DEFAULT_CATEGORY_RULES))

    def _paths(self, run_now=None):
        value = run_now if run_now is not None else self._run_now()
        return RadarPaths(self.workspace, value.date().isoformat())

    @staticmethod
    def _run_lock_target(paths):
        return paths.root / "运行状态" / "完整报告运行"

    def _history(self, paths):
        root = paths.root / "运行状态" / "历史指标"
        return self.services.load_history(root) if root.exists() else []

    def _source(self, name, call, statuses, warnings):
        try:
            rows = call()
        except SourceError:
            statuses.append({"source": name, "status": "degraded"})
            warnings.append("{} 来源暂不可用，已安全降级。".format(name))
            return []
        statuses.append({"source": name, "status": "ok"})
        return rows

    def _details(self, names, statuses, warnings, fallback_limit=0):
        details, detail_successes, failures = {}, [], []
        fallback_attempts, fallback_successes, fallback_failures = [], [], []
        api_rate_limited = False
        for index, name in enumerate(names):
            try:
                if api_rate_limited:
                    raise SourceError(
                        "GitHub detail quota unavailable", status_code=429
                    )
                details[name] = self.services.fetch_details(name)
                detail_successes.append(name)
            except SourceError as exc:
                failures.append(name)
                if exc.status_code in {403, 429}:
                    api_rate_limited = True
                if index >= fallback_limit:
                    continue
                fallback_attempts.append(name)
                try:
                    details[name] = self.services.fetch_star_count(name)
                    fallback_successes.append(name)
                except SourceError:
                    fallback_failures.append(name)
        state = (
            "ok" if not failures
            else "partial" if detail_successes
            else "degraded"
        )
        statuses.append({"source": "github_details", "status": state})
        if failures:
            warnings.append("GitHub 仓库详情部分不可用，已保留其余来源数据。")
        if fallback_attempts:
            fallback_state = (
                "ok" if not fallback_failures
                else "partial" if fallback_successes
                else "degraded"
            )
            statuses.append({
                "source": "github_star_fallback", "status": fallback_state,
            })
            if fallback_failures:
                warnings.append(
                    "部分增长榜项目的当前累计 Star 公开页面兜底失败。"
                )
        return details

    @staticmethod
    def _detail_names(github, day, week, trending, top):
        """Prioritize detail quota for rows users see in growth rankings."""
        ordered = []
        seen = set()

        def add(records):
            for record in records:
                key = record.full_name.casefold()
                if key not in seen:
                    seen.add(key)
                    ordered.append(record.full_name)

        add(sorted(
            (record for record in day if record.stars_24h_external is not None),
            key=lambda record: (-record.stars_24h_external, record.full_name),
        )[:top])
        add(sorted(
            (record for record in week if record.stars_7d_external is not None),
            key=lambda record: (-record.stars_7d_external, record.full_name),
        )[:top])
        add(trending[:top])
        add(github[:top])
        add(day[:top])
        add(week[:top])
        return ordered

    def _local_metrics(self, records, history_rows, now):
        grouped = {}
        for row in history_rows:
            grouped.setdefault(row["repo"], []).append(row)
        for record in records:
            if record.total_stars is None:
                continue
            samples = [
                {
                    "at": row.get("at")
                    or row["date"] + "T00:00:00+00:00",
                    "stars": row["stars"],
                }
                for row in grouped.get(record.full_name, [])
                if row["date"] != now.date().isoformat()
            ]
            samples.append({"at": now.isoformat(), "stars": record.total_stars})
            record.stars_24h_local = calculate_local_growth(samples, 1)
            record.stars_7d_local = calculate_local_growth(samples, 7)
            record.growth_acceleration = calculate_growth_acceleration(
                record.stars_24h_local, record.stars_7d_local
            )

    @staticmethod
    def _ranking(
        record, value, source, metric_name, unit, description_overrides=None
    ):
        override = (description_overrides or {}).get(record.full_name, {})
        return {
            "repo": record.full_name,
            "value": value,
            "total_stars": record.total_stars,
            "source": source,
            "data_scope": _data_scope(source),
            "metric_name": metric_name,
            "unit": unit,
            "description": record.description,
            "primary_category": record.primary_category,
            "repository_type": record.repository_type,
            "purpose": _repository_purpose(record, override),
            "problem_solved": _problem_solved(record, override),
        }

    def _rankings(self, selected, statuses, top, description_overrides=None):
        status_map = {row["source"]: row["status"] for row in statuses}
        total_records = []
        if status_map.get("github_top") == "ok":
            total_records = sorted(
                (
                    record for record in selected
                    if record.total_stars is not None
                    and _has_source(record, "github", "search")
                ),
                key=lambda record: (-record.total_stars, record.full_name),
            )[:top]
        total = []
        for record in total_records:
            total.append(self._ranking(
                record, record.total_stars, "GitHub total stars", "当前累计 Star", "个",
                description_overrides,
            ))
        daily, weekly = [], []
        for record in selected:
            if (
                status_map.get("ossinsight_24h") == "ok"
                and record.stars_24h_external is not None
                and _has_source(record, "ossinsight", period="24h")
            ):
                daily.append(self._ranking(
                    record, record.stars_24h_external,
                    "OSSInsight 24h external", "过去24小时新增 Star", "个",
                    description_overrides,
                ))
            elif record.stars_24h_local is not None:
                daily.append(self._ranking(
                    record, record.stars_24h_local,
                    "local history 24h", "过去24小时新增 Star", "个",
                    description_overrides,
                ))
            if (
                status_map.get("ossinsight_7d") == "ok"
                and record.stars_7d_external is not None
                and _has_source(record, "ossinsight", period="7d")
            ):
                weekly.append(self._ranking(
                    record, record.stars_7d_external,
                    "OSSInsight 7d external", "过去7天新增 Star", "个",
                    description_overrides,
                ))
            elif record.stars_7d_local is not None:
                weekly.append(self._ranking(
                    record, record.stars_7d_local,
                    "local history 7d", "过去7天新增 Star", "个",
                    description_overrides,
                ))
        daily.sort(key=lambda row: (-row["value"], row["repo"]))
        weekly.sort(key=lambda row: (-row["value"], row["repo"]))
        acceleration = [
            self._ranking(
                record, record.growth_acceleration,
                "local history", "24h增速相对7日日均", "倍",
                description_overrides,
            )
            for record in selected if record.growth_acceleration is not None
        ]
        acceleration.sort(key=lambda row: (-row["value"], row["repo"]))
        return {
            "total": total,
            "daily": daily[:top],
            "weekly": weekly[:top],
            "acceleration": acceleration[:top],
        }, total_records

    def _category_and_reuse(
        self, selected, top, now, pinned_repositories=None,
        statuses=None, warnings=None, description_overrides=None,
    ):
        categories = {}
        for record in selected:
            categories.setdefault(record.primary_category, []).append(record)
        category_trends = [
            {
                "category": category,
                "facts": ["本期纳入 {} 个仓库。".format(len(items))],
                "change": None,
                "confidence": "中",
            }
            for category, items in sorted(categories.items())
        ]
        pinned = {
            str(name).strip().casefold()
            for name in (pinned_repositories or [])
        }
        candidates = [
            record for record in selected
            if record.repository_type not in {"Awesome 清单", "教程或课程"}
        ]
        scored = [(record, _reusability(record, now)) for record in candidates]
        scored.sort(key=lambda item: (-item[1]["score"], item[0].full_name))
        chosen = scored[:top]
        chosen_names = {record.full_name for record, _ in chosen}
        for item in scored:
            record, _ = item
            if record.full_name not in pinned or record.full_name in chosen_names:
                continue
            replace_at = next(
                (
                    index for index in range(len(chosen) - 1, -1, -1)
                    if chosen[index][0].full_name not in pinned
                ),
                None,
            )
            if replace_at is not None:
                chosen[replace_at] = item
                chosen_names.add(record.full_name)
        chosen.sort(key=lambda item: (-item[1]["score"], item[0].full_name))
        readmes, readme_failures = {}, []
        if os.environ.get("RADAR_SKIP_README") == "1":
            readme_failures = [record.full_name for record, _ in chosen]
        else:
            for record, _ in chosen:
                try:
                    readmes[record.full_name] = self.services.fetch_readme(record.full_name)
                except SourceError:
                    readme_failures.append(record.full_name)
        if statuses is not None:
            state = (
                "ok" if not readme_failures
                else "partial" if readmes else "degraded"
            )
            statuses.append({"source": "github_readme", "status": state})
        if readme_failures and warnings is not None:
            warnings.append("部分项目 README 功能或应用场景不可用，已回退到分类摘要。")
        reusable = []
        for record, scoring in chosen:
            risks = list(scoring["risks"])
            if scoring["missing_signals"]:
                risks.append("部分复用信号缺失")
            if record.full_name in pinned:
                risks.append("固定关注项目；数据不足时不代表实时复用评分")
            readme = readmes.get(record.full_name, {})
            features = readme.get("features", [])[:6]
            use_cases = readme.get("use_cases", [])[:6]
            override = (description_overrides or {}).get(record.full_name, {})
            features_are_chinese = features and all(_has_chinese(item) for item in features)
            use_cases_are_chinese = use_cases and all(_has_chinese(item) for item in use_cases)
            purpose = (
                override.get("用途与定位")
                or (
                    "核心功能：" + "；".join(features)
                    if features_are_chinese else _reusable_purpose(record)
                )
            )
            problem_solved = (
                override.get("主要解决问题")
                or (
                    "应用场景：" + "；".join(use_cases)
                    if use_cases_are_chinese else _reusable_problem(record)
                )
            )
            if override:
                description_source = "人工校准中文摘要（依据官方 README）"
            elif features_are_chinese or use_cases_are_chinese:
                description_source = readme.get("source", ["中文 README"])[0]
            elif features or use_cases:
                description_source = "英文 README 暂无可靠翻译，已回退中文分类摘要"
            else:
                description_source = "分类摘要"
            reusable.append({
                "repo": record.full_name,
                "repository_type": record.repository_type,
                "runnable": record.repository_type not in {
                    "Awesome 清单", "教程或课程"
                },
                "purpose": purpose,
                "problem_solved": problem_solved,
                "description_source": description_source,
                "total_stars": record.total_stars,
                "reuse": override.get("可复用内容") or _reusable_content(record),
                "integration": "源码或发行版评估",
                "score": scoring["score"],
                "score_components": scoring["components"],
                "score_signals": scoring["signals"],
                "missing_signals": scoring["missing_signals"],
                "score_confidence": scoring["confidence"],
                "license": record.license or "未明确",
                "maintenance": _maintenance(record, now),
                "risks": risks,
                "actions": ["先验证许可证、文档和集成成本"],
            })
        return category_trends, reusable

    def _cooling_section(
        self, records, history_rows, watch_rows, rankings, current_date=None
    ):
        previous_date = max((row["date"] for row in history_rows), default=None)
        previous = [
            row for row in history_rows if row.get("date") == previous_date
        ]
        previous_ranks = {
            row["repo"]: row["rank"]
            for row in previous if isinstance(row.get("rank"), int)
        }
        current_ranks = {
            row["repo"]: index
            for index, row in enumerate(rankings["total"], 1)
        }
        previous_growth = {
            row["repo"]: row.get(
                "stars_7d_external", row.get("stars_7d_local")
            )
            for row in previous
        }
        current_growth = {
            row["repo"]: row["value"] for row in rankings["weekly"]
        }
        growth_pairs = {
            repo: (value, current_growth[repo])
            for repo, value in previous_growth.items()
            if isinstance(value, (int, float)) and repo in current_growth
        }
        rank_absence_observable = len(rankings["total"]) >= 50
        effective_current_ranks = dict(current_ranks)
        if not rank_absence_observable:
            for repo in set(previous_ranks) | {
                row["仓库"] for row in watch_rows
            }:
                effective_current_ranks.setdefault(repo, 50)

        def category_positions(rows, rank_lookup=None):
            grouped = {}
            for row in rows:
                repo = row.get("repo")
                category = row.get("primary_category")
                rank = (
                    rank_lookup.get(repo)
                    if rank_lookup is not None and repo in rank_lookup
                    else row.get("rank")
                )
                if (
                    isinstance(repo, str)
                    and isinstance(category, str)
                    and category
                    and type(rank) is int
                    and rank > 0
                ):
                    grouped.setdefault(category, {})[repo] = rank
            positions = {}
            for category, repo_ranks in grouped.items():
                ordered = sorted(
                    repo_ranks.items(), key=lambda item: (item[1], item[0])
                )
                for position, (repo, _rank) in enumerate(ordered, 1):
                    positions[repo] = (category, position)
            return positions

        previous_category_ranks = category_positions(previous)
        current_category_ranks = category_positions(
            [
                {
                    "repo": record.full_name,
                    "primary_category": record.primary_category,
                }
                for record in records
            ],
            current_ranks,
        )
        category_rank_drops = {}
        for repo in set(previous_category_ranks) & set(current_category_ranks):
            previous_category, previous_rank = previous_category_ranks[repo]
            current_category, current_rank = current_category_ranks[repo]
            if previous_category == current_category:
                category_rank_drops[repo] = current_rank - previous_rank

        history_growth = {}
        for row in history_rows:
            repo, row_date = row.get("repo"), row.get("date")
            if (
                not isinstance(repo, str)
                or not isinstance(row_date, str)
                or row_date == current_date
            ):
                continue
            value = row.get("stars_7d_external")
            if type(value) not in {int, float} or (
                type(value) is float and not math.isfinite(value)
            ):
                value = row.get("stars_7d_local")
            if type(value) in {int, float} and not (
                type(value) is float and not math.isfinite(value)
            ):
                history_growth[(repo, row_date)] = value

        consecutive_slowdown = {}
        for repo, value in current_growth.items():
            points = sorted(
                (
                    (row_date, growth)
                    for (history_repo, row_date), growth in history_growth.items()
                    if history_repo == repo
                ),
                key=lambda item: item[0],
            )
            points.append((current_date or "9999-12-31", value))
            points.sort(key=lambda item: item[0])
            if len(points) >= 3:
                consecutive_slowdown[repo] = [
                    growth for _date, growth in points[-3:]
                ]
        cooling = detect_cooling(
            previous_ranks,
            effective_current_ranks,
            growth_pairs,
            category_rank_drops=category_rank_drops,
            consecutive_slowdown=consecutive_slowdown,
            watchlist=[row["仓库"] for row in watch_rows],
        )
        record_map = {record.full_name: record for record in records}
        candidate = next(
            (record for record in records if record.stars_7d_external is not None),
            None,
        )
        result = []
        if not rank_absence_observable:
            for row in watch_rows:
                repo = row["仓库"]
                if repo not in current_ranks:
                    result.append({
                        "repo": repo,
                        "facts": ["有效榜单覆盖不足 50，暂不判断退榜。"],
                        "inferences": ["数据不足不等于项目过时。"],
                        "evidence": [],
                        "confidence": "低",
                        "replacement": "证据不足",
                        "actions": ["继续观察，待覆盖足够后再判断"],
                    })
        for signal in cooling:
            old = record_map.get(signal["repo"])
            if old is None:
                old = classify_repository(
                    RepositoryRecord(full_name=signal["repo"]),
                    copy.deepcopy(DEFAULT_CATEGORY_RULES),
                )
            relation = analyze_replacement(old, candidate or old, [])
            result.append({
                "repo": signal["repo"],
                "facts": [
                    "检测到退榜或降温信号：{}。".format(signal["reason_code"])
                ],
                "inferences": ["退榜不等于项目过时，需继续核验。"],
                "evidence": [],
                "confidence": relation["confidence"],
                "replacement": relation["relation"],
                "actions": ["检查发布、Issue 和迁移证据后再决策"],
            })
        return result

    def _objective_parts(
        self, records, statuses, args, now, rules=None, warnings=None
    ):
        selected = [
            record for record in records
            if args.category is None or record.primary_category == args.category
        ]
        descriptions = (rules or {}).get("中文描述覆盖", {})
        rankings, total_records = self._rankings(
            selected, statuses, args.top, descriptions
        )
        category_trends, reusable = self._category_and_reuse(
            selected,
            args.top,
            now,
            (rules or {}).get("复用榜固定项目", []),
            statuses,
            warnings,
            descriptions,
        )
        return selected, rankings, total_records, category_trends, reusable

    def _model(
        self, records, history_rows, watch_rows, statuses, warnings, args, now,
        objective_parts=None, tracking_records=None,
    ):
        if objective_parts is None:
            objective_parts = self._objective_parts(
                records, statuses, args, now
            )
        selected, rankings, total_records, category_trends, reusable = (
            objective_parts
        )
        reusable.sort(key=lambda x: (-x.get("score", 0), x.get("repo", "")))
        featured_candidates = []
        weekly_map = {r["repo"]: r for r in rankings.get("weekly", [])}
        daily_map = {r["repo"]: r for r in rankings.get("daily", [])}
        _priority_categories = {"AI", "开发工具"}
        for record in total_records:
            cat = record.primary_category or "其他"
            weekly_entry = weekly_map.get(record.full_name)
            weekly_gain = weekly_entry["value"] if weekly_entry else 0
            daily_entry = daily_map.get(record.full_name)
            daily_gain = daily_entry["value"] if daily_entry else 0
            if cat in _priority_categories and weekly_gain >= 500:
                featured_candidates.append(
                    (record, "weekly", weekly_gain, weekly_entry)
                )
            elif cat in _priority_categories and daily_gain >= 2:
                featured_candidates.append(
                    (record, "daily", daily_gain, daily_entry)
                )
        if len(featured_candidates) < 3:
            for record in total_records:
                if not any(
                    candidate[0].full_name == record.full_name
                    for candidate in featured_candidates
                ):
                    featured_candidates.append(
                        (record, "total", record.total_stars or 0, None)
                    )
                if len(featured_candidates) >= 5:
                    break
        source_priority = {"weekly": 0, "daily": 1, "total": 2}
        featured_candidates.sort(
            key=lambda item: (
                source_priority[item[1]], -item[2], item[0].full_name
            )
        )
        featured = []
        for record, source_type, gain_value, ranking_entry in featured_candidates[:5]:
            if source_type == "weekly":
                fact_line = "周增 Star：+{}；总 Star：{}".format(gain_value, record.total_stars or 0)
                inf_line = "本周增长显著，值得深入评估复用价值。"
            elif source_type == "daily":
                fact_line = "日增 Star：+{}；总 Star：{}".format(gain_value, record.total_stars or 0)
                inf_line = "近期活跃度上升，建议跟踪观察。"
            else:
                fact_line = "总 Star：{}".format(record.total_stars or 0)
                inf_line = "综合热度较高，可作为参考基准。"
            evidence = []
            if ranking_entry is not None:
                evidence.append({
                    "type": "ranking_data",
                    "source": ranking_entry.get("source", "unknown"),
                    "period": "7d" if source_type == "weekly" else "24h",
                    "value": gain_value,
                    "unit": "个",
                })
            evidence.extend(
                {
                    "type": "source_record",
                    "source": item.get("source", "unknown"),
                }
                for item in record.source_records
                if isinstance(item, Mapping) and item.get("source")
            )
            featured.append({
                "repo": record.full_name,
                "facts": [fact_line],
                "inferences": [inf_line],
                "evidence": evidence,
                "confidence": record.data_confidence or "中",
                "actions": ["进入观察清单或开展小范围验证"],
            })
        watch_section = [
            {
                "repo": row["仓库"],
                "reason": str(
                    row.get("备注") or row.get("使用场景") or "人工观察项目"
                ),
                "actions": ["按观察清单设置继续检查"],
            }
            for row in watch_rows
        ]

        facts = ["本期合并并去重 {} 个仓库。".format(len(selected))]
        if not history_rows:
            facts.append(
                "暂无本地历史；本次不计算 local 增量、增长加速度或历史退榜。"
            )
        if args.category:
            facts.append("当前为分类 {} 的筛选结果。".format(args.category))
        return {
            "metadata": {
                "date": now.date().isoformat(),
                "query_time": now.isoformat(),
                "periods": {
                    "total": "GitHub 当前累计 Star",
                    "daily": "OSSInsight 24h external；缺失时仅使用 local history",
                    "weekly": "OSSInsight 7d external；缺失时仅使用 local history",
                    "acceleration": "local 24h / (local 7d / 7)",
                },
                "ranking_sources": {
                    "total": "GitHub",
                    "daily": "OSSInsight 或 local history",
                    "weekly": "OSSInsight 或 local history",
                    "acceleration": "local history",
                },
                "source_status": copy.deepcopy(statuses),
            },
            "overview": {
                "facts": facts,
                "inferences": self._generate_overview_inferences(
                    category_trends, rankings
                ),
                "actions": ["优先验证高复用分项目，保留证据缺口。"],
            },
            "rankings": rankings,
            "category_trends": category_trends,
            "reusable_projects": reusable,
            "featured_projects": featured,
            "history": self._cooling_section(
                tracking_records if tracking_records is not None else selected,
                history_rows,
                watch_rows,
                rankings,
                current_date=now.date().isoformat(),
            ),
            "market_trends": self._generate_market_trends(
                category_trends, rankings
            ),
            "watchlist": watch_section,
            "data_quality": {
                "source_status": copy.deepcopy(statuses),
                "query_time": now.isoformat(),
                "periods": {
                    "24h": "external 与 local 严格分字段",
                    "7d": "external 与 local 严格分字段",
                    "trending": "selected set，仅作发现集合，不作精确全量排行",
                },
                "warnings": copy.deepcopy(warnings),
                "missing": [],
                "conflicts": [],
                "manual_inferences": ["推断与事实、行动已分栏。"],
            },
            "repositories": [record.to_dict() for record in selected],
        }

    def _generate_market_trends(self, category_trends, rankings):
        trends = []
        weekly_list = rankings.get("weekly", [])
        ai_weekly = [r for r in weekly_list if r.get("primary_category") == "AI"]
        dev_weekly = [r for r in weekly_list if r.get("primary_category") == "开发工具"]
        ai_weekly_total = sum(r.get("value", 0) for r in ai_weekly)
        dev_weekly_total = sum(r.get("value", 0) for r in dev_weekly)
        total_weekly = sum(r.get("value", 0) for r in weekly_list)
        if ai_weekly and ai_weekly_total > 1000:
            top_ai = sorted(ai_weekly, key=lambda x: -x.get("value", 0))[:3]
            top_names = "、".join(r["repo"].split("/")[-1] for r in top_ai)
            top_ai_total = sum(r.get("value", 0) for r in top_ai)
            pct = round(top_ai_total / total_weekly * 100) if total_weekly > 0 else 0
            trends.append({
                "conclusion": (
                    "AI 类项目本周增长活跃，{} 周增合计 {} 个 Star，"
                    "约占榜单总增量的 {}%。"
                ).format(top_names, top_ai_total, pct),
                "evidence": [
                    {
                        "type": "ranking_data",
                        "source": row.get("source", "unknown"),
                        "period": "7d",
                        "repo": row.get("repo", ""),
                        "value": row.get("value", 0),
                        "unit": "个",
                    }
                    for row in top_ai
                ],
                "change": None,
                "confidence": "中",
                "consecutive_periods": 1,
            })
        if dev_weekly and dev_weekly_total > 500:
            trends.append({
                "conclusion": "开发工具类项目过去 7 日新增 Star 合计 {} 个。".format(
                    dev_weekly_total
                ),
                "evidence": [
                    {
                        "type": "ranking_data",
                        "source": row.get("source", "unknown"),
                        "period": "7d",
                        "repo": row.get("repo", ""),
                        "value": row.get("value", 0),
                        "unit": "个",
                    }
                    for row in dev_weekly
                ],
                "change": None,
                "confidence": "低",
                "consecutive_periods": 1,
            })
        non_ai_weekly = [r for r in weekly_list if r.get("primary_category") not in ("AI", "开发工具")]
        non_ai_total = sum(r.get("value", 0) for r in non_ai_weekly)
        if non_ai_total > ai_weekly_total and non_ai_total > dev_weekly_total:
            trends.append({
                "conclusion": (
                    "非 AI 与开发工具类项目过去 7 日新增 Star 合计 {} 个，"
                    "占据当前榜单增量主体。"
                ).format(non_ai_total),
                "evidence": [
                    {
                        "type": "ranking_data",
                        "source": row.get("source", "unknown"),
                        "period": "7d",
                        "repo": row.get("repo", ""),
                        "value": row.get("value", 0),
                        "unit": "个",
                    }
                    for row in non_ai_weekly
                ],
                "change": None,
                "confidence": "中",
                "consecutive_periods": 1,
            })
        return trends

    def _generate_overview_inferences(self, category_trends, rankings):
        inferences = ["榜单热度不等同于项目适配度。"]
        weekly_list = rankings.get("weekly", [])
        ai_weekly = [r for r in weekly_list if r.get("primary_category") == "AI"]
        if ai_weekly:
            top_ai = sorted(ai_weekly, key=lambda x: -x.get("value", 0))[:2]
            names = "、".join(r["repo"].split("/")[-1] for r in top_ai)
            inferences.append("AI 方向本周增长集中，{} 等项目增量突出。".format(names))
        total_weekly = sum(r.get("value", 0) for r in weekly_list)
        if total_weekly > 10000:
            inferences.append("本周整体增量较高，开源社区活跃度上升。")
        return inferences
    def run_report(self, args):
        try:
            run_now = self._run_now()
            paths = self._paths(run_now)
            if should_save(args):
                with exclusive_file_lock(self._run_lock_target(paths)):
                    return self._run_report_once(args, paths, run_now)
            return self._run_report_once(args, paths, run_now)
        except Exception:
            _safe_error("报告运行失败；未保存未完成的产物。")
            return 2

    def _run_report_once(self, args, paths, run_now):
        collected = self._collect_report(args, paths=paths, now=run_now)
        if collected is None:
            return 2
        paths, model, markdown, warnings, records, watch_updates = collected
        if not should_save(args):
            print(markdown, end="")
            for warning in warnings:
                _safe_error("警告：{}".format(warning))
            return 0
        return self._save_report(
            paths,
            model,
            markdown,
            warnings,
            records,
            watch_updates,
            update_state=args.category is None,
            category=args.category,
        )

    def _collect_report(self, args, paths=None, now=None):
        if now is None:
            now = self._run_now()
        if paths is None:
            paths = self._paths(now)
        rules = ensure_default_category_rules(paths.categories_file)
        validate_fixed_primary_categories(rules, paths.categories_file)
        if (
            args.category is not None
            and args.category not in FIXED_PRIMARY_CATEGORIES
        ):
            raise ValueError("category must be a fixed primary category")
        statuses, warnings = [], []
        github = self._source(
            "github_top",
            lambda: self.services.fetch_top(top=args.top),
            statuses,
            warnings,
        )
        day = self._source(
            "ossinsight_24h",
            lambda: self.services.fetch_trend("24h"),
            statuses,
            warnings,
        )
        week = self._source(
            "ossinsight_7d",
            lambda: self.services.fetch_trend("7d"),
            statuses,
            warnings,
        )
        trending = self._source(
            "github_trending_weekly",
            lambda: self.services.fetch_trending(period="weekly"),
            statuses,
            warnings,
        )
        discoveries = github + day + week + trending
        names = self._detail_names(github, day, week, trending, args.top)
        details = self._details(
            names, statuses, warnings, fallback_limit=min(len(names), args.top * 2)
        )
        all_rows = discoveries + list(details.values())
        records = merge_records(all_rows) if all_rows else []
        for record in records:
            classify_repository(record, rules)
        history_rows = self._history(paths)
        if args.category is not None:
            history_rows = [
                row for row in history_rows
                if row.get("primary_category") == args.category
            ]
        self._local_metrics(records, history_rows, now)

        # Freeze every objective section before consulting the human-maintained
        # watchlist.  This prevents watched-only repositories from changing any
        # market ranking, category count, or reuse result.
        objective_parts = self._objective_parts(
            records, statuses, args, now, rules, warnings
        )
        watch_rows = (
            self.services.read_watchlist(paths.watchlist_file)
            if paths.watchlist_file.exists()
            or (should_save(args) and args.category is None)
            else []
        )
        if not all_rows and not history_rows and not watch_rows:
            _safe_error(
                "无法构成有意义的报告：发现来源均不可用且无本地依据。"
            )
            return None

        watch_names = {
            row["仓库"]
            for row in watch_rows if row.get("是否检查更新") == "是"
        }
        cache = {record.full_name: record for record in records}
        tracking_records = list(records)
        watch_updates, watch_failures = {}, []
        for row in watch_rows:
            if (
                args.category is not None
                or row.get("是否检查更新") != "是"
            ):
                continue
            name = row["仓库"]
            record = cache.get(name)
            if record is None or not _has_source(
                record, "github", "repository_detail"
            ):
                try:
                    record = copy.deepcopy(self.services.fetch_details(name))
                except SourceError:
                    watch_failures.append(name)
                    continue
                classify_repository(record, rules)
                if name not in cache:
                    self._local_metrics([record], history_rows, now)
                    tracking_records.append(record)
            watch_updates[name] = _watch_update(record, now)
        if watch_failures:
            state = "partial" if watch_updates else "degraded"
            statuses.append({"source": "watchlist_details", "status": state})
            warnings.append(
                "观察清单部分仓库暂不可用，未生成对应自动字段。"
            )
        elif watch_names:
            statuses.append({"source": "watchlist_details", "status": "ok"})
        model = self._model(
            records,
            history_rows,
            watch_rows,
            statuses,
            warnings,
            args,
            now,
            objective_parts=objective_parts,
            tracking_records=tracking_records,
        )
        markdown = build_report(model)
        return (
            paths,
            model,
            markdown,
            warnings,
            tracking_records,
            watch_updates,
        )

    def _save_report(
        self, paths, model, markdown, warnings, records, watch_updates,
        update_state=True, category=None,
    ):
        archive_report, archive_data, latest_report, latest_data = (
            _report_targets(paths, category)
        )
        try:
            self.services.save_report(
                archive_report, archive_data, markdown, model
            )
        except Exception:
            _safe_error(
                "归档报告保存失败；未执行历史或观察清单更新。"
            )
            return 3
        try:
            self.services.save_report(
                latest_report, latest_data, markdown, model
            )
        except Exception:
            _safe_error(
                "最新报告保存失败；归档已保留，未执行历史或观察清单更新。"
            )
            return 3

        if category is None:
            try:
                html_output = build_html_report(model)
                for html_path in (paths.archive_html, paths.latest_html):
                    html_path.parent.mkdir(parents=True, exist_ok=True)
                    html_path.write_text(html_output, encoding="utf-8")
                    if html_path.stat().st_size == 0:
                        raise OSError("HTML report was empty")
            except Exception:
                _safe_error(
                    "HTML 报告保存失败；Markdown 与 JSON 已保留，未执行历史或观察清单更新。"
                )
                return 3
            try:
                cleanup_stale_latest_reports(latest_report.parent, paths.run_date)
            except Exception:
                _safe_error(
                    "最新报告旧文件清理失败；当日报告已保留，未执行历史或观察清单更新。"
                )
                return 3

        if not update_state:
            print(latest_report)
            for warning in warnings:
                _safe_error("警告：{}".format(warning))
            return 0

        ranks = {
            row["repo"]: index
            for index, row in enumerate(model["rankings"]["total"], 1)
        }
        history_output = []
        for record in records:
            if record.total_stars is None:
                continue
            row = {
                "repo": record.full_name,
                "stars": record.total_stars,
                "at": model["metadata"]["query_time"],
                "primary_category": record.primary_category,
            }
            if record.full_name in ranks:
                row["rank"] = ranks[record.full_name]
            for field in (
                "stars_24h_external",
                "stars_7d_external",
                "stars_24h_local",
                "stars_7d_local",
            ):
                value = getattr(record, field)
                if value is not None:
                    row[field] = value
            history_output.append(row)
        try:
            self.services.upsert_history(
                paths.history_file, paths.run_date, history_output
            )
            if watch_updates:
                self.services.update_watchlist(
                    paths.watchlist_file, watch_updates
                )
        except Exception:
            _safe_error(
                "报告已保存但后置更新失败；请检查历史指标或观察清单。"
            )
            return 4
        print(latest_report)
        for warning in warnings:
            _safe_error("警告：{}".format(warning))
        return 0

    def run_repo(self, full_name):
        try:
            record = self._classified_detail(full_name)
            _validate_record_numbers(record)
            result = {
                "repository": record.to_dict(),
                "reusability": _reusability(
                    record, self._run_now()
                ),
            }
            print(_strict_json(result))
            return 0
        except SourceError:
            _safe_error("仓库来源暂不可用。")
            return 2
        except Exception:
            _safe_error("仓库输入或处理失败。")
            return 2

    def run_compare(self, old, new):
        try:
            old_record = self._classified_detail(old)
            new_record = self._classified_detail(new)
            trends = {
                record.full_name: record
                for record in self.services.fetch_trend("7d")
            }
            if old_record.full_name in trends:
                old_record = merge_records(
                    [old_record, trends[old_record.full_name]]
                )[0]
                classify_repository(old_record, copy.deepcopy(DEFAULT_CATEGORY_RULES))
            if new_record.full_name in trends:
                new_record = merge_records(
                    [new_record, trends[new_record.full_name]]
                )[0]
                classify_repository(new_record, copy.deepcopy(DEFAULT_CATEGORY_RULES))
            _validate_record_numbers(old_record)
            _validate_record_numbers(new_record)
            relation = analyze_replacement(old_record, new_record, [])
            old_tags = {
                str(value).strip().casefold()
                for value in old_record.secondary_tags
            }
            new_tags = {
                str(value).strip().casefold()
                for value in new_record.secondary_tags
            }
            now = self._run_now()
            result = {
                "old": {
                    "repository": old_record.to_dict(),
                    "reusability": _reusability(old_record, now),
                    "heat": {
                        "total_stars": old_record.total_stars,
                        "stars_7d_external": old_record.stars_7d_external,
                    },
                },
                "new": {
                    "repository": new_record.to_dict(),
                    "reusability": _reusability(new_record, now),
                    "heat": {
                        "total_stars": new_record.total_stars,
                        "stars_7d_external": new_record.stars_7d_external,
                    },
                },
                "tag_overlap": sorted(old_tags & new_tags),
                "replacement": relation,
                "evidence_gaps": [
                    "未采集迁移说明、官方对比、Release、Issue 或社区讨论证据"
                ],
            }
            print(_strict_json(result))
            return 0
        except SourceError:
            _safe_error("对比所需来源暂不可用。")
            return 2
        except Exception:
            _safe_error("对比输入或处理失败。")
            return 2

    def run_watchlist(self):
        try:
            now = self._run_now()
            paths = self._paths(now)
            rows = self.services.read_watchlist(paths.watchlist_file)
            targets = [
                row for row in rows if row.get("是否检查更新") == "是"
            ]
            updates, failures = {}, []
            for row in targets:
                name = row["仓库"]
                try:
                    record = self.services.fetch_details(name)
                except SourceError:
                    failures.append(name)
                    continue
                updates[name] = _watch_update(record, now)
            updated = (
                self.services.update_watchlist(paths.watchlist_file, updates)
                if updates else 0
            )
            result = {
                "checked": len(targets),
                "failed_repositories": failures,
                "status": (
                    "degraded" if failures and not updates
                    else "partial" if failures else "ok"
                ),
                "updated": updated,
            }
            print(_strict_json(result))
            return 1 if failures else 0
        except Exception:
            _safe_error("观察清单读取或更新失败；未提交不完整更新。")
            return 2


def run_report(args):
    return RadarApp().run_report(args)


def run_repo(full_name):
    return RadarApp().run_repo(full_name)


def run_compare(old, new):
    return RadarApp().run_compare(old, new)


def run_watchlist():
    return RadarApp().run_watchlist()


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.workspace is not None and args.output_root is not None:
        sys.stderr.write("error: --workspace and --output-root cannot be used together\n")
        return 2
    if args.output_root is not None:
        app = RadarApp(workspace=args.output_root)
        handlers = {
            "report": lambda: app.run_report(args),
            "repo": lambda: app.run_repo(args.full_name),
            "compare": lambda: app.run_compare(args.old, args.new),
            "watchlist": app.run_watchlist,
        }
    elif args.workspace is None:
        handlers = {
            "report": lambda: run_report(args),
            "repo": lambda: run_repo(args.full_name),
            "compare": lambda: run_compare(args.old, args.new),
            "watchlist": run_watchlist,
        }
    else:
        app = RadarApp(workspace=args.workspace / OUTPUT_DIRECTORY)
        handlers = {
            "report": lambda: app.run_report(args),
            "repo": lambda: app.run_repo(args.full_name),
            "compare": lambda: app.run_compare(args.old, args.new),
            "watchlist": app.run_watchlist,
        }
    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
