"""Filesystem layout for GitHub trend radar runs."""

from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
import tempfile
from typing import Any, Dict

import yaml

from models import RepositoryRecord


DEFAULT_CATEGORY_RULES: Dict[str, Dict[str, Any]] = {
    "一级分类": {
        "AI": {
            "ossinsight_collections": ["AI", "Artificial Intelligence"],
            "topics": ["ai", "artificial-intelligence", "machine-learning", "llm"],
            "keywords": ["artificial intelligence", "large language model", "machine learning"],
        },
        "开发工具": {
            "ossinsight_collections": ["Developer Tools"],
            "topics": ["developer-tools", "cli", "ide"],
            "keywords": ["developer tool", "coding assistant", "command line"],
        },
        "基础设施与 DevOps": {
            "ossinsight_collections": ["DevOps", "Cloud Native"],
            "topics": ["devops", "kubernetes", "docker", "ci-cd"],
            "keywords": ["cloud native", "continuous integration", "infrastructure"],
        },
        "数据与数据库": {
            "ossinsight_collections": ["Database", "Data Engineering"],
            "topics": ["database", "data-engineering", "analytics"],
            "keywords": ["database", "data engineering", "analytics"],
        },
        "安全": {
            "ossinsight_collections": ["Security"],
            "topics": ["security", "cybersecurity", "privacy"],
            "keywords": ["cybersecurity", "application security", "privacy"],
        },
        "前端与 UI": {
            "ossinsight_collections": ["Frontend", "Design Tools"],
            "topics": ["frontend", "ui", "web-components"],
            "keywords": ["user interface", "front end", "design system"],
        },
        "移动端": {
            "ossinsight_collections": ["Mobile"],
            "topics": ["mobile", "android", "ios"],
            "keywords": ["mobile application", "android", "ios"],
        },
        "其他": {},
    },
    "二级标签": {
        "AI Agent": {"topics": ["ai-agent", "agents"], "keywords": ["ai agent"]},
        "Coding Agent": {"topics": ["coding-agent"], "keywords": ["coding agent"]},
        "MCP": {"topics": ["mcp"], "keywords": ["model context protocol"]},
        "RAG": {"topics": ["rag"], "keywords": ["retrieval augmented generation"]},
        "Agent Memory": {"topics": ["agent-memory"], "keywords": ["agent memory"]},
        "模型推理": {"topics": ["inference", "llm-inference"], "keywords": ["model inference"]},
        "多模态": {"topics": ["multimodal"], "keywords": ["multi modal", "multimodal"]},
        "TTS": {"topics": ["tts", "text-to-speech"], "keywords": ["text to speech"]},
        "AI 视频": {"topics": ["ai-video", "text-to-video"], "keywords": ["ai video", "text to video"]},
        "CLI": {"topics": ["cli"], "keywords": ["command line"]},
        "IDE": {"topics": ["ide"], "keywords": ["integrated development environment"]},
        "CI/CD": {"topics": ["ci-cd", "continuous-integration"], "keywords": ["continuous integration", "continuous delivery"]},
    },
    "人工覆盖": {},
    "复用榜固定项目": [],
    "中文描述覆盖": {},
}

# This is a product boundary, not a user-editable default.  The YAML file may
# tune matching rules and secondary tags, but report requests and manual
# overrides must stay inside these primary categories.
FIXED_PRIMARY_CATEGORIES = tuple(DEFAULT_CATEGORY_RULES["一级分类"].keys())


MATCH_RULE_FIELDS = {
    "ossinsight_collections",
    "collection_names",
    "collections",
    "ossinsight",
    "topics",
    "github_topics",
    "keywords",
    "keyword",
}
TOP_LEVEL_FIELDS = {
    "版本", "一级分类", "二级标签", "人工覆盖", "复用榜固定项目",
    "中文描述覆盖",
}
OVERRIDE_FIELDS = {"一级分类", "二级标签"}
DESCRIPTION_OVERRIDE_FIELDS = {"用途与定位", "主要解决问题", "可复用内容"}


def _schema_error(path: Path, field: str, requirement: str) -> ValueError:
    return ValueError(f"分类规则 {path}：字段 {field} {requirement}")


def _validate_terms(path: Path, field: str, value: Any) -> None:
    if isinstance(value, str) and value.strip():
        return
    if isinstance(value, list) and all(
        isinstance(item, str) and item.strip() for item in value
    ):
        return
    raise _schema_error(path, field, "必须是非空 str 或 list[str]")


def _validate_rule_section(path: Path, section_name: str, section: Any) -> None:
    if not isinstance(section, dict):
        raise _schema_error(path, section_name, "必须是 dict")
    for label, rule in section.items():
        field = f"{section_name}.{label}"
        if not isinstance(label, str) or not label.strip():
            raise _schema_error(path, section_name, "的名称必须是非空 str")
        if not isinstance(rule, dict):
            raise _schema_error(path, field, "必须是 dict")
        for rule_field, value in rule.items():
            if rule_field not in MATCH_RULE_FIELDS:
                raise _schema_error(path, f"{field}.{rule_field}", "不是支持的规则字段")
            _validate_terms(path, f"{field}.{rule_field}", value)


def _validate_overrides(path: Path, overrides: Any) -> None:
    if not isinstance(overrides, dict):
        raise _schema_error(path, "人工覆盖", "必须是 dict")
    for full_name, override in overrides.items():
        base_field = f"人工覆盖.{full_name}"
        if not isinstance(full_name, str) or not full_name.strip():
            raise _schema_error(path, "人工覆盖", "的仓库名必须是非空 str")
        try:
            RepositoryRecord(full_name=full_name)
        except ValueError:
            raise _schema_error(
                path, base_field, "必须是合法的 owner/repo"
            ) from None
        if not isinstance(override, dict) or not override:
            raise _schema_error(path, base_field, "必须是非空 dict")
        unknown_fields = set(override) - OVERRIDE_FIELDS
        if unknown_fields:
            unknown = sorted(str(field) for field in unknown_fields)[0]
            raise _schema_error(path, f"{base_field}.{unknown}", "不允许")
        primary = override.get("一级分类")
        if not isinstance(primary, str) or not primary.strip():
            raise _schema_error(path, f"{base_field}.一级分类", "必须是非空 str")
        if "二级标签" in override:
            tags = override["二级标签"]
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) and tag.strip() for tag in tags
            ):
                raise _schema_error(
                    path, f"{base_field}.二级标签", "必须是 list[str]"
                )


def validate_category_rules(
    rules: Any, path: Path = Path("<memory>")
) -> Dict[str, Any]:
    """Validate one in-memory rule document against the strict whitelist."""

    rules_path = Path(path)
    if not isinstance(rules, dict):
        raise _schema_error(rules_path, "<root>", "必须是 dict")
    unknown_fields = set(rules) - TOP_LEVEL_FIELDS
    if unknown_fields:
        unknown = sorted(str(field) for field in unknown_fields)[0]
        raise _schema_error(rules_path, unknown, "不允许")
    for section_name in ("一级分类", "二级标签"):
        if section_name not in rules:
            raise _schema_error(rules_path, section_name, "缺失")
        _validate_rule_section(rules_path, section_name, rules[section_name])
    if "人工覆盖" in rules:
        _validate_overrides(rules_path, rules["人工覆盖"])
    pinned = rules.get("复用榜固定项目", [])
    if not isinstance(pinned, list):
        raise _schema_error(rules_path, "复用榜固定项目", "必须是 list[str]")
    for index, full_name in enumerate(pinned):
        try:
            RepositoryRecord(full_name=full_name)
        except (TypeError, ValueError, AttributeError):
            raise _schema_error(
                rules_path,
                "复用榜固定项目.{}".format(index),
                "必须是合法的 owner/repo",
            ) from None
    descriptions = rules.get("中文描述覆盖", {})
    if not isinstance(descriptions, dict):
        raise _schema_error(rules_path, "中文描述覆盖", "必须是 dict")
    for full_name, override in descriptions.items():
        base_field = "中文描述覆盖.{}".format(full_name)
        try:
            RepositoryRecord(full_name=full_name)
        except (TypeError, ValueError, AttributeError):
            raise _schema_error(rules_path, base_field, "必须是合法的 owner/repo") from None
        if not isinstance(override, dict) or not override:
            raise _schema_error(rules_path, base_field, "必须是非空 dict")
        unknown = set(override) - DESCRIPTION_OVERRIDE_FIELDS
        if unknown:
            raise _schema_error(
                rules_path, "{}.{}".format(base_field, sorted(unknown)[0]), "不允许"
            )
        for field, value in override.items():
            if not isinstance(value, str) or not value.strip():
                raise _schema_error(rules_path, "{}.{}".format(base_field, field), "必须是非空 str")
    return rules


def validate_fixed_primary_categories(
    rules: Any, path: Path = Path("<memory>")
) -> Dict[str, Any]:
    """Enforce the radar's fixed primary-category product boundary."""

    validated = validate_category_rules(rules, path)
    configured = set(validated["一级分类"])
    expected = set(FIXED_PRIMARY_CATEGORIES)
    if configured != expected:
        raise _schema_error(
            Path(path),
            "一级分类",
            "必须保留固定分类，仅可调整匹配规则",
        )
    for full_name, override in validated.get("人工覆盖", {}).items():
        primary = override["一级分类"]
        if primary not in expected:
            raise _schema_error(
                Path(path),
                f"人工覆盖.{full_name}.一级分类",
                "必须使用固定分类",
            )
    return validated


def load_category_rules(path: Path) -> Dict[str, Any]:
    """Load category rules with safe parsing and strict deterministic schema."""

    rules_path = Path(path)
    try:
        yaml_text = rules_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError(f"分类规则 {rules_path}：无法安全读取 UTF-8 文件") from None

    try:
        rules = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f"line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else "line unknown, column unknown"
        )
        raise ValueError(
            f"分类规则 {rules_path}：YAML 格式或标签无效（{location}）"
        ) from None

    return validate_category_rules(rules, rules_path)


def ensure_default_category_rules(path: Path) -> Dict[str, Any]:
    """Publish defaults atomically without ever replacing an existing target."""

    rules_path = Path(path)
    try:
        if rules_path.exists():
            return load_category_rules(rules_path)
        rules_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ValueError(f"分类规则 {rules_path}：无法准备配置目录") from None

    yaml_text = yaml.safe_dump(
        DEFAULT_CATEGORY_RULES, allow_unicode=True, sort_keys=False
    )
    temporary_path: Path = None  # type: ignore[assignment]
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{rules_path.name}.",
            suffix=".tmp",
            dir=str(rules_path.parent),
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(yaml_text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        try:
            os.link(temporary_path, rules_path)
        except FileExistsError:
            pass
    except OSError:
        raise ValueError(f"分类规则 {rules_path}：原子写入失败") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return load_category_rules(rules_path)


@dataclass(frozen=True)
class RadarPaths:
    workspace: Path
    run_date: str

    def __post_init__(self) -> None:
        try:
            parsed_run_date = date.fromisoformat(self.run_date)
        except ValueError as exc:
            raise ValueError("run_date must use the YYYY-MM-DD format") from exc

        if parsed_run_date.isoformat() != self.run_date:
            raise ValueError("run_date must use the YYYY-MM-DD format")

    @property
    def root(self) -> Path:
        return self.workspace

    @property
    def config_dir(self) -> Path:
        return self.root / "配置"

    @property
    def watchlist_file(self) -> Path:
        return self.config_dir / "项目观察清单.xlsx"

    @property
    def categories_file(self) -> Path:
        return self.config_dir / "分类规则.yaml"

    @property
    def latest_report(self) -> Path:
        return self.root / "最新报告" / f"GitHub开源趋势与项目复用雷达-{self.run_date}.md"

    @property
    def latest_data(self) -> Path:
        return self.root / "最新报告" / f"原始数据-{self.run_date}.json"

    @property
    def latest_html(self) -> Path:
        return self.root / "最新报告" / f"GitHub开源趋势与项目复用雷达-{self.run_date}.html"

    @property
    def archive_dir(self) -> Path:
        year, month, _ = self.run_date.split("-", 2)
        return self.root / "历史归档" / year / month / self.run_date

    @property
    def archive_report(self) -> Path:
        return self.archive_dir / f"GitHub开源趋势与项目复用雷达-{self.run_date}.md"

    @property
    def archive_data(self) -> Path:
        return self.archive_dir / f"原始数据-{self.run_date}.json"

    @property
    def archive_html(self) -> Path:
        return self.archive_dir / f"GitHub开源趋势与项目复用雷达-{self.run_date}.html"

    @property
    def history_file(self) -> Path:
        year_month = self.run_date[:7]
        return self.root / "运行状态" / "历史指标" / f"{year_month}.jsonl"

    @property
    def history_index(self) -> Path:
        return self.root / "运行状态" / "历史索引.json"

    @property
    def locks_dir(self) -> Path:
        return self.root / "运行状态" / "锁"
