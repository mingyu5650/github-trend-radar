#!/usr/bin/env python3
"""Tests for Markdown report rendering and paired persistence."""

import copy
import json
import math
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from report import _number, build_report  # noqa: E402
import storage  # noqa: E402
from storage import (  # noqa: E402
    StorageError,
    save_complete_report,
    secure_target_path,
)


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


class EvilText(str):
    def __str__(self):
        return "[injected](javascript:alert(1))"

    def __format__(self, format_spec):
        return "<script>injected</script>"

    def strip(self, *args, **kwargs):
        return "2026-07-14"


class EvilInt(int):
    def __format__(self, format_spec):
        return "[injected](javascript:alert(1))"


class EvilObject:
    def __str__(self):
        return "<script>[injected](javascript:alert(1))</script>"


def sample_model():
    return {
        "metadata": {
            "date": "2026-07-14",
            "query_time": "2026-07-14T08:30:00+08:00",
            "periods": {
                "total": "查询时累计值",
                "daily": "2026-07-13T08:30:00+08:00 ~ 2026-07-14T08:30:00+08:00",
                "weekly": "2026-07-07T08:30:00+08:00 ~ 2026-07-14T08:30:00+08:00",
            },
            "source_status": [
                {"source": "GitHub API", "status": "ok"},
                {"source": "OSS Insight", "status": "degraded"},
            ],
        },
        "overview": {
            "facts": ["周榜新增项目保持活跃"],
            "inferences": ["工具链整合仍在加速"],
            "actions": ["试用 org/repo"],
        },
        "rankings": {
            "total": [
                {"repo": "zeta/one", "value": 9000, "repository_type": "可运行项目"},
                {"repo": "alpha/two", "value": 12000, "repository_type": "SDK 或库"},
            ],
            "daily": [{"repo": "org/daily", "growth": 88}],
            "weekly": [{"repo": "org/repo", "growth": 1234}],
            "acceleration": [{"repo": "org/fast", "value": 2.5}],
        },
        "category_trends": [
            {"category": "AI Agent", "facts": ["上榜 8 个"], "change": 2, "confidence": "高"}
        ],
        "reusable_projects": [
            {
                "repo": "org/reusable",
                "repository_type": "可运行项目",
                "runnable": True,
                "reuse": "Agent 调度器",
                "integration": "Python SDK",
                "score": 86,
                "license": "Apache-2.0",
                "maintenance": "活跃",
                "risks": ["接口变动"],
                "actions": ["小流量试用"],
            },
            {
                "repo": "org/list",
                "repository_type": "信息聚合仓库",
                "runnable": False,
                "reuse": "列表",
                "integration": "阅读",
                "score": 100,
                "license": "MIT",
                "maintenance": "活跃",
                "risks": [],
                "actions": [],
            },
        ],
        "featured_projects": [
            {
                "repo": "org/repo",
                "facts": ["七日新增 1234 Star"],
                "inferences": ["开发者需求上升"],
                "evidence": [{"type": "weekly", "detail": "OSS Insight 周榜"}],
                "confidence": "中",
                "actions": ["验证兼容性"],
            }
        ],
        "history": [
            {
                "repo": "old/tool",
                "facts": ["从前 20 移出前 50"],
                "inferences": ["关注度可能分流"],
                "evidence": [{"type": "rank", "detail": "20 -> 51"}],
                "confidence": "中",
                "replacement": "注意力分流，非直接替代",
                "actions": ["继续观察"],
            }
        ],
        "market_trends": [
            {
                "conclusion": "Agent 工具增长",
                "evidence": [{"metric": "weekly_growth", "value": 1234}],
                "change": 18,
                "confidence": "中",
                "consecutive_periods": 2,
            }
        ],
        "watchlist": [
            {"repo": "org/repo", "reason": "周增长显著", "actions": ["试用"]}
        ],
        "data_quality": {
            "source_status": [{"source": "GitHub API", "status": "ok"}],
            "query_time": "2026-07-14T08:30:00+08:00",
            "periods": ["24h external", "7d external", "local snapshot"],
            "warnings": ["OSS Insight 部分降级"],
            "missing": ["org/missing 缺少 license"],
            "conflicts": ["external 与 local 不同口径"],
            "manual_inferences": ["退榜原因为人工推断"],
        },
    }


def _save_worker(report_path, data_path, marker, start_event):
    model = sample_model()
    model["overview"]["facts"] = [marker]
    start_event.wait()
    save_complete_report(report_path, data_path, build_report(model), model)


class NumberTests(unittest.TestCase):
    def test_formats_only_valid_finite_numbers(self):
        self.assertEqual(_number(None), "—")
        self.assertEqual(_number(1234), "1,234")
        self.assertEqual(_number(2.5), "2.5")
        self.assertEqual(_number(1234567.8901234567), "1,234,567.8901234567")
        self.assertEqual(_number(Decimal("1234567.890123456789")), "1,234,567.890123456789")
        self.assertRegex(_number(Decimal("1e1000000")), r"^1E\+1000000$")
        for value in (True, math.nan, math.inf, -math.inf, "1234", object()):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    _number(value)
        with self.assertRaises(ValueError):
            _number(EvilInt(1234))


class ReportRenderingTests(unittest.TestCase):
    def test_rankings_explain_metric_units_repository_purpose_and_problem(self):
        model = sample_model()
        common = {
            "primary_category": "开发工具",
            "repository_type": "可运行软件",
            "description": "AI coding CLI",
            "purpose": "开发工具领域的可运行软件。官方简介：AI coding CLI",
            "problem_solved": "提升开发、调试、代码理解或自动化效率。",
            "data_scope": "OSSInsight 外部统计",
        }
        model["rankings"]["total"] = [
            dict(common, repo="org/tool", value=2000, data_scope="GitHub 当前累计值")
        ]
        model["rankings"]["daily"] = [
            dict(common, repo="org/tool", value=6, total_stars=2000)
        ]
        model["rankings"]["weekly"] = [
            dict(common, repo="org/tool", value=69, total_stars=2000)
        ]
        model["rankings"]["acceleration"] = [
            dict(common, repo="org/tool", value=2.3, data_scope="本地历史快照计算")
        ]

        rendered = build_report(model)

        self.assertIn("当前累计 Star（个）", rendered)
        self.assertIn("过去24小时新增 Star（个）", rendered)
        self.assertIn("过去7天新增 Star（个）", rendered)
        self.assertIn("24h增速相对7日日均（倍）", rendered)
        self.assertIn("| 2.30 | 本地历史快照计算 |", rendered)
        self.assertNotIn("| 序号 | 仓库 | 数值 |", rendered)
        self.assertIn("| 1 | [org/tool](https://github.com/org/tool)", rendered)
        self.assertIn("| 2,000 | +6 | OSSInsight 外部统计 |", rendered)
        self.assertIn("| 2,000 | +69 | OSSInsight 外部统计 |", rendered)
        self.assertIn("开发工具领域的可运行软件。官方简介：AI coding CLI", rendered)
        self.assertIn("提升开发、调试、代码理解或自动化效率。", rendered)

    def test_renders_all_nine_fixed_sections_and_model_weekly_number(self):
        rendered = build_report(sample_model())
        self.assertTrue(rendered.startswith("# GitHub 趋势雷达日报（2026-07-14）"))
        for title in SECTION_TITLES:
            self.assertEqual(rendered.count("# {}\n".format(title)), 1)
        self.assertIn("org/repo", rendered)
        self.assertIn("1,234", rendered)
        self.assertIn("许可证 20、维护 20、文档 15、Release 15、集成 15、社区 10、CI 5", rendered)
        self.assertIn("90 天内为“活跃”", rendered)
        self.assertIn("91–365 天为“需复核”", rendered)
        self.assertIn("超过 365 天为“低活跃”", rendered)

    def test_preserves_ranking_array_order_and_does_not_modify_input(self):
        model = sample_model()
        before = copy.deepcopy(model)
        rendered_once = build_report(model)
        rendered_twice = build_report(model)
        self.assertLess(rendered_once.index("zeta/one"), rendered_once.index("alpha/two"))
        self.assertEqual(rendered_once, rendered_twice)
        self.assertEqual(model, before)

    def test_separates_facts_inferences_evidence_confidence_and_actions(self):
        rendered = build_report(sample_model())
        for label in ("事实", "推断原因", "证据", "可信度", "行动建议", "替代关系"):
            self.assertIn(label, rendered)
        self.assertIn("退榜不等于过时", rendered)

    def test_suppresses_direct_replacement_claim_when_evidence_is_empty(self):
        model = sample_model()
        model["history"][0]["replacement"] = "直接替代"
        model["history"][0]["evidence"] = []
        with self.assertRaises(ValueError):
            build_report(model)

    def test_direct_replacement_requires_canonical_whitelisted_evidence(self):
        model = sample_model()
        model["history"][0]["replacement"] = "直接替代"
        model["history"][0]["evidence"] = [{"type": "README", "detail": "claim"}]
        with self.assertRaises(ValueError):
            build_report(model)

        model["history"][0]["evidence"] = [
            {"type": "migration_guide", "detail": "official migration"}
        ]
        rendered = build_report(model)
        self.assertIn("| 直接替代 |", rendered)

        model["history"][0]["replacement"] = "非直接替代"
        model["history"][0]["evidence"] = []
        self.assertIn("| 非直接替代 |", build_report(model))

    def test_direct_replacement_gate_uses_stripped_relation(self):
        model = sample_model()
        model["history"][0]["replacement"] = "  直接替代  "
        model["history"][0]["evidence"] = []
        with self.assertRaises(ValueError):
            build_report(model)

        model["history"][0]["evidence"] = [
            {"type": "official_comparison", "detail": "official"}
        ]
        rendered = build_report(model)
        self.assertIn("| 直接替代 |", rendered)
        self.assertNotIn("|   直接替代", rendered)

    def test_relation_rejects_unicode_control_and_format_characters_before_gate(self):
        for control in ("\x00", "\u200b", "\u2060"):
            for evidence in (
                [],
                [{"type": "official_comparison", "detail": "official"}],
            ):
                model = sample_model()
                model["history"][0]["replacement"] = (
                    control + "直接替代" + control
                )
                model["history"][0]["evidence"] = evidence
                with self.subTest(control=repr(control), evidence=bool(evidence)):
                    with self.assertRaises(ValueError) as caught:
                        build_report(model)
                    self.assertNotIn("\x00", str(caught.exception))
                    self.assertNotIn("\u200b", str(caught.exception))
                    self.assertNotIn("\u2060", str(caught.exception))

    def test_empty_arrays_render_explicit_degradation_notes(self):
        model = sample_model()
        model["overview"] = {"facts": [], "inferences": [], "actions": []}
        model["rankings"] = {"total": [], "daily": [], "weekly": []}
        for key in (
            "category_trends", "reusable_projects", "featured_projects",
            "history", "market_trends", "watchlist",
        ):
            model[key] = []
        model["data_quality"] = {
            "source_status": [], "warnings": [], "missing": [],
            "conflicts": [], "manual_inferences": [],
        }
        rendered = build_report(model)
        self.assertGreaterEqual(rendered.count("暂无可用数据"), 7)
        self.assertIn("数据降级", rendered)

    def test_escapes_table_cells_and_rejects_unsafe_repo_links(self):
        model = sample_model()
        model["rankings"]["weekly"] = [
            {
                "repo": "[click](javascript:alert(1))<img src=x onerror=alert(1)>",
                "repo_url": "javascript:alert(1)",
                "growth": 5,
                "source": "bad|source & *bold* _italic_ ~strike~ `code`\nnext\x01\u0085hidden",
            },
            {
                "repo": "safe/repo",
                "repo_url": "https://evil.test/[inject](javascript:alert(1))",
                "growth": 4,
            },
        ]
        rendered = build_report(model)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("[click](javascript:", rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn(r"\[click\]\(javascript:alert\(1\)\)", rendered)
        self.assertIn("bad\\|source", rendered)
        self.assertIn("&amp;", rendered)
        self.assertIn("nexthidden", rendered)
        self.assertIn(r"\*bold\*", rendered)
        self.assertIn(r"\_italic\_", rendered)
        self.assertIn(r"\~strike\~", rendered)
        self.assertIn(r"\`code\`", rendered)
        self.assertNotIn("\u0085", rendered)
        self.assertIn("[safe/repo](https://github.com/safe/repo)", rendered)

    def test_rejects_non_mapping_entries_in_every_section_array(self):
        for section in (
            "category_trends", "reusable_projects", "featured_projects",
            "history", "market_trends", "watchlist",
        ):
            model = sample_model()
            model[section] = ["invalid-entry"]
            with self.subTest(section=section), self.assertRaises(ValueError):
                build_report(model)

    def test_reusable_board_excludes_non_runnable_information_repository(self):
        rendered = build_report(sample_model())
        self.assertIn("org/reusable", rendered)
        self.assertNotIn("org/list", rendered)

    def test_featured_projects_are_capped_at_five(self):
        model = sample_model()
        model["featured_projects"] = [
            {
                "repo": "org/item{}".format(i),
                "facts": ["fact"],
                "inferences": [],
                "evidence": [],
                "confidence": "低",
                "actions": [],
            }
            for i in range(7)
        ]
        rendered = build_report(model)
        self.assertIn("org/item4", rendered)
        self.assertNotIn("org/item5", rendered)

    def test_rejects_bad_metadata_structure_and_ranking_types(self):
        cases = []
        missing_metadata = sample_model()
        del missing_metadata["metadata"]
        cases.append(missing_metadata)
        bad_date = sample_model()
        bad_date["metadata"]["date"] = "14/07/2026"
        cases.append(bad_date)
        missing_rankings = sample_model()
        del missing_rankings["rankings"]
        cases.append(missing_rankings)
        bad_ranking = sample_model()
        bad_ranking["rankings"]["weekly"] = {}
        cases.append(bad_ranking)
        missing_row_field = sample_model()
        missing_row_field["rankings"]["weekly"] = [{"repo": "org/repo"}]
        cases.append(missing_row_field)
        unknown_ranking = sample_model()
        unknown_ranking["rankings"]["mystery"] = []
        cases.append(unknown_ranking)
        bad_category_entry = sample_model()
        bad_category_entry["category_trends"] = ["not-a-mapping"]
        cases.append(bad_category_entry)
        bad_featured_entry = sample_model()
        bad_featured_entry["featured_projects"] = [{"facts": ["missing repo"]}]
        cases.append(bad_featured_entry)
        bad_market_metric = sample_model()
        bad_market_metric["market_trends"][0]["change"] = True
        cases.append(bad_market_metric)
        bad_score = sample_model()
        bad_score["reusable_projects"][0]["score"] = "86"
        cases.append(bad_score)
        for model in cases:
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    build_report(model)

    def test_sensitive_keys_are_rejected_recursively_at_build_entry(self):
        model = sample_model()
        model["metadata"]["source_status"][0]["Authorization"] = "Bearer private"
        model["data_quality"]["headers"] = {"X-Token": "private"}
        with self.assertRaises(ValueError):
            build_report(model)

        model = sample_model()
        model["overview"]["accessToken"] = "private"
        with self.assertRaises(ValueError):
            build_report(model)

    def test_rejects_invalid_calendar_date_even_when_shape_looks_iso(self):
        model = sample_model()
        model["metadata"]["date"] = "2026-99-99"
        with self.assertRaises(ValueError):
            build_report(model)

    def test_rejects_bool_and_nonfinite_declared_metrics(self):
        for value in (True, math.nan, math.inf, -math.inf, "123"):
            model = sample_model()
            model["rankings"]["weekly"][0]["growth"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_report(model)

    def test_rejects_behavior_rewriting_scalar_subclasses_before_use(self):
        models = []
        evil_repo = sample_model()
        evil_repo["rankings"]["weekly"][0]["repo"] = EvilText("org/repo")
        models.append(evil_repo)
        evil_date = sample_model()
        evil_date["metadata"]["date"] = EvilText("not-a-date")
        models.append(evil_date)
        evil_url = sample_model()
        evil_url["rankings"]["weekly"][0]["repo_url"] = EvilText("https://github.com/org/repo")
        models.append(evil_url)
        evil_number = sample_model()
        evil_number["rankings"]["weekly"][0]["growth"] = EvilInt(1234)
        models.append(evil_number)
        for model in models:
            with self.subTest(model=model):
                with self.assertRaises(ValueError) as caught:
                    build_report(model)
                self.assertNotIn("javascript", str(caught.exception))
                self.assertNotIn("script", str(caught.exception))

    def test_rejects_non_ascii_or_unsupported_model_keys(self):
        for key in ("t\u043eken", "中文键", "bad.key", EvilText("safe_key")):
            model = sample_model()
            model[key] = "secret-value"
            with self.subTest(key=repr(key)), self.assertRaises(ValueError):
                build_report(model)

    def test_rejects_bad_optional_repository_type_with_value_safe_error(self):
        model = sample_model()
        model["reusable_projects"][0]["repository_type"] = EvilInt(1)
        with self.assertRaises(ValueError) as caught:
            build_report(model)
        self.assertNotIn("javascript", str(caught.exception))

    def test_rejects_bad_optional_text_fields_used_by_renderer(self):
        cases = []
        bad_source = sample_model()
        bad_source["rankings"]["weekly"][0]["source"] = 7
        cases.append(bad_source)
        bad_query_time = sample_model()
        bad_query_time["metadata"]["query_time"] = 7
        cases.append(bad_query_time)
        bad_status = sample_model()
        bad_status["metadata"]["source_status"][0]["status"] = 7
        cases.append(bad_status)
        for model in cases:
            with self.subTest(model=model), self.assertRaises(ValueError):
                build_report(model)

    def test_preflight_rejects_depth_nodes_and_text_limits(self):
        depth_65 = sample_model()
        nested = None
        for _ in range(65):
            nested = [nested]
        depth_65["extra"] = nested

        depth_1100 = sample_model()
        nested = None
        for _ in range(1100):
            nested = [nested]
        depth_1100["extra"] = nested

        too_many_nodes = sample_model()
        too_many_nodes["extra"] = [None] * 200001

        long_text = sample_model()
        long_text["overview"]["facts"] = ["x" * (1024 * 1024 + 1)]

        for model in (depth_65, depth_1100, too_many_nodes, long_text):
            with self.subTest(kind=list(model)[-1]), self.assertRaises(ValueError):
                build_report(model)

    def test_rejects_non_json_leaves_containers_and_text_list_objects(self):
        mutations = (
            lambda model: model.update({"extra": EvilObject()}),
            lambda model: model.update({"extra": {"bad"}}),
            lambda model: model.update({"extra": ("tuple",)}),
            lambda model: model["overview"]["facts"].append(EvilObject()),
            lambda model: model.update({"extra_decimal": Decimal("1.5")}),
        )
        for mutate in mutations:
            model = sample_model()
            mutate(model)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ValueError) as caught:
                    build_report(model)
                self.assertNotIn("javascript", str(caught.exception))

    def test_decimal_allowance_is_field_scoped_even_when_object_is_reused(self):
        model = sample_model()
        shared_decimal = Decimal("12.5")
        model["rankings"]["weekly"][0]["growth"] = shared_decimal
        model["extra_decimal"] = shared_decimal
        with self.assertRaises(ValueError):
            build_report(model)


class PairedStorageTests(unittest.TestCase):
    def test_business_locks_are_centralized_under_runtime_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Path(tmp) / "最新报告"
            report_path = paths / "GitHub开源趋势与项目复用雷达-2026-07-14.md"
            data_path = paths / "原始数据-2026-07-14.json"
            model = sample_model()
            save_complete_report(report_path, data_path, build_report(model), model)

            lock_files = list((Path(tmp) / "运行状态" / "锁").glob("*.lock"))
            self.assertTrue(lock_files)
            self.assertFalse(report_path.with_name(report_path.name + ".lock").exists())
            self.assertFalse(data_path.with_name(data_path.name + ".lock").exists())

    def test_rejects_empty_bundle_without_touching_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            report.write_text("old-report", encoding="utf-8")
            data.write_text('{"old": true}', encoding="utf-8")
            with self.assertRaises(ValueError):
                save_complete_report(report, data, "", {})
            self.assertEqual(report.read_text(encoding="utf-8"), "old-report")
            self.assertEqual(data.read_text(encoding="utf-8"), '{"old": true}')

    def test_rejects_same_date_markdown_that_is_not_canonical_model_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            report.write_text("old-report", encoding="utf-8")
            data.write_text("old-data", encoding="utf-8")
            markdown = build_report(sample_model()).replace("周榜新增", "篡改内容", 1)
            with self.assertRaises(ValueError):
                save_complete_report(report, data, markdown, sample_model())
            self.assertEqual(report.read_text(encoding="utf-8"), "old-report")
            self.assertEqual(data.read_text(encoding="utf-8"), "old-data")

    def test_rejects_business_lock_targets_before_inspection_or_locking(self):
        for lock_name in ("bundle.lock", "bundle.LOCK"):
            with self.subTest(lock_name=lock_name), tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / "bundle"
                data = Path(tmp) / lock_name
                data.write_text("held-lock-sentinel", encoding="utf-8")
                with patch("storage.os.lstat", side_effect=AssertionError("must not inspect")):
                    with self.assertRaises(ValueError) as caught:
                        save_complete_report(
                            report,
                            data,
                            build_report(sample_model()),
                            sample_model(),
                        )
                self.assertNotIn(lock_name, str(caught.exception))
                self.assertFalse(report.exists())
                self.assertEqual(data.read_text(encoding="utf-8"), "held-lock-sentinel")
                self.assertFalse((Path(tmp) / (lock_name + ".lock")).exists())

    def test_public_target_validation_rejects_lock_business_file_but_normal_targets_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                secure_target_path(Path(tmp) / "workbook.xlsx.lock")
            normal = secure_target_path(Path(tmp) / "workbook.xlsx")
            self.assertEqual(normal.name, "workbook.xlsx")

    def test_unicode_confusable_key_is_rejected_before_any_files_are_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "nested" / "latest.md"
            data = Path(tmp) / "nested" / "data.json"
            model = sample_model()
            model["t\u043eken"] = "secret-value"
            with self.assertRaises(ValueError):
                save_complete_report(report, data, build_report(sample_model()), model)
            self.assertFalse(report.parent.exists())

    def test_saves_matching_markdown_and_utf8_json_without_modifying_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "nested" / "latest.md"
            data = Path(tmp) / "other" / "data.json"
            model = sample_model()
            before = copy.deepcopy(model)
            markdown = build_report(model)
            save_complete_report(report, data, markdown, model)
            self.assertEqual(report.read_text(encoding="utf-8"), markdown)
            self.assertEqual(json.loads(data.read_text(encoding="utf-8")), model)
            self.assertIn("周榜新增", data.read_text(encoding="utf-8"))
            self.assertEqual(model, before)
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o600)

    def test_safely_serializes_decimal_in_declared_numeric_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            model = sample_model()
            model["rankings"]["weekly"][0]["growth"] = Decimal("1234.567890123456789")
            markdown = build_report(model)
            save_complete_report(report, data, markdown, model)
            persisted = json.loads(data.read_text(encoding="utf-8"), parse_float=Decimal)
            self.assertEqual(
                persisted["rankings"]["weekly"][0]["growth"],
                Decimal("1234.567890123456789"),
            )

    def test_storage_rejects_behavior_rewriting_and_non_json_values_without_touching_old(self):
        models = []
        evil_repo = sample_model()
        evil_repo["rankings"]["weekly"][0]["repo"] = EvilText("org/repo")
        models.append(evil_repo)
        evil_number = sample_model()
        evil_number["rankings"]["weekly"][0]["growth"] = EvilInt(1234)
        models.append(evil_number)
        custom_leaf = sample_model()
        custom_leaf["extra"] = EvilObject()
        models.append(custom_leaf)
        tuple_leaf = sample_model()
        tuple_leaf["extra"] = ("tuple",)
        models.append(tuple_leaf)
        for model in models:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / "latest.md"
                data = Path(tmp) / "data.json"
                report.write_text("old-report", encoding="utf-8")
                data.write_text("old-data", encoding="utf-8")
                with self.assertRaises(ValueError):
                    save_complete_report(report, data, build_report(sample_model()), model)
                self.assertEqual(report.read_text(encoding="utf-8"), "old-report")
                self.assertEqual(data.read_text(encoding="utf-8"), "old-data")

    def test_rejects_date_mismatch_sensitive_keys_nonfinite_and_nonserializable(self):
        cases = []
        mismatch = sample_model()
        cases.append((build_report(mismatch).replace("2026-07-14", "2026-07-15", 1), mismatch))
        sensitive = sample_model()
        sensitive["nested"] = {"Api_Token": "private"}
        cases.append((build_report(sample_model()), sensitive))
        camel_sensitive = sample_model()
        camel_sensitive["nested"] = {"accessToken": "private"}
        cases.append((build_report(sample_model()), camel_sensitive))
        nonfinite = sample_model()
        nonfinite["market_trends"][0]["change"] = math.nan
        cases.append((build_report(sample_model()), nonfinite))
        nonserializable = sample_model()
        nonserializable["bad"] = object()
        cases.append((build_report(sample_model()), nonserializable))
        invalid_calendar = sample_model()
        invalid_calendar["metadata"]["date"] = "2026-99-99"
        cases.append((build_report(sample_model()).replace("2026-07-14", "2026-99-99", 1), invalid_calendar))
        bool_metric = sample_model()
        bool_metric["rankings"]["weekly"][0]["growth"] = True
        cases.append((build_report(sample_model()), bool_metric))
        for markdown, model in cases:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / "latest.md"
                data = Path(tmp) / "data.json"
                report.write_text("old", encoding="utf-8")
                data.write_text("old", encoding="utf-8")
                with self.assertRaises((ValueError, TypeError)):
                    save_complete_report(report, data, markdown, model)
                self.assertEqual(report.read_text(encoding="utf-8"), "old")
                self.assertEqual(data.read_text(encoding="utf-8"), "old")

    def test_first_and_second_replace_failure_restore_old_pair_and_clean_artifacts(self):
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / "latest.md"
                data = Path(tmp) / "data.json"
                report.write_text("# GitHub 趋势雷达日报（2026-07-14）\nold", encoding="utf-8")
                data.write_text('{"old": true}', encoding="utf-8")
                old_report = report.read_bytes()
                old_data = data.read_bytes()
                real_replace = os.replace
                count = {"value": 0}

                def fail_selected(source, target):
                    count["value"] += 1
                    if count["value"] == fail_at:
                        raise OSError("replace failed")
                    return real_replace(source, target)

                with patch("storage.os.replace", side_effect=fail_selected):
                    with self.assertRaises(StorageError):
                        save_complete_report(report, data, build_report(sample_model()), sample_model())
                self.assertEqual(report.read_bytes(), old_report)
                self.assertEqual(data.read_bytes(), old_data)
                self.assertEqual(list(Path(tmp).glob(".*.tmp")), [])
                self.assertEqual(list(Path(tmp).glob(".*.bak")), [])

    def test_hardlink_backup_restores_original_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            report.write_text("# GitHub 趋势雷达日报（2026-07-14）\nold", encoding="utf-8")
            data.write_text('{"old": true}', encoding="utf-8")
            old_inode = report.stat().st_ino
            real_replace = os.replace
            calls = {"value": 0}

            def fail_second(source, target):
                calls["value"] += 1
                if calls["value"] == 2:
                    raise OSError("second commit failed")
                return real_replace(source, target)

            with patch("storage.os.replace", side_effect=fail_second):
                with self.assertRaises(StorageError):
                    save_complete_report(report, data, build_report(sample_model()), sample_model())
            self.assertEqual(report.stat().st_ino, old_inode)

    def test_incomplete_rollback_retains_private_recovery_backup_and_reports_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            old_report = "# GitHub 趋势雷达日报（2026-07-14）\nold"
            report.write_text(old_report, encoding="utf-8")
            data.write_text('{"old": true}', encoding="utf-8")
            real_replace = os.replace
            calls = {"value": 0}

            def fail_commit_and_rollback(source, target):
                calls["value"] += 1
                if calls["value"] in (2, 3):
                    raise OSError("replace failed")
                return real_replace(source, target)

            with patch("storage.os.replace", side_effect=fail_commit_and_rollback):
                with self.assertRaises(StorageError) as caught:
                    save_complete_report(report, data, build_report(sample_model()), sample_model())
            recovery = sorted(set(Path(tmp).glob("*.recovery.bak")))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].read_text(encoding="utf-8"), old_report)
            self.assertEqual(stat.S_IMODE(recovery[0].stat().st_mode), 0o600)
            self.assertIn(str(recovery[0]), str(caught.exception))

    def test_second_replace_failure_restores_missing_first_target_to_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            data.write_text('{"old": true}', encoding="utf-8")
            old_data = data.read_bytes()
            real_install = storage._install_stage
            count = {"value": 0}

            def fail_second(stage, target, existed):
                count["value"] += 1
                if count["value"] == 2:
                    raise OSError("replace failed")
                return real_install(stage, target, existed)

            with patch("storage._install_stage", side_effect=fail_second):
                with self.assertRaises(StorageError):
                    save_complete_report(report, data, build_report(sample_model()), sample_model())
            self.assertFalse(report.exists())
            self.assertEqual(data.read_bytes(), old_data)

    def test_updates_preserve_modes_and_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            report.write_text("old", encoding="utf-8")
            data.write_text("old", encoding="utf-8")
            report.chmod(0o640)
            data.chmod(0o604)
            save_complete_report(report, data, build_report(sample_model()), sample_model())
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o604)

            real = Path(tmp) / "real.md"
            alias = Path(tmp) / "alias.md"
            real.write_text("sentinel", encoding="utf-8")
            alias.symlink_to(real)
            with self.assertRaises((ValueError, StorageError)):
                save_complete_report(alias, data, build_report(sample_model()), sample_model())
            self.assertEqual(real.read_text(encoding="utf-8"), "sentinel")

    def test_rejects_hardlink_samefile_and_casefold_alias_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.md"
            alias = Path(tmp) / "alias.json"
            first.write_text("same inode", encoding="utf-8")
            os.link(first, alias)
            with self.assertRaises(ValueError):
                save_complete_report(first, alias, build_report(sample_model()), sample_model())

        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "Report.md"
            alias = Path(tmp) / "report.MD"
            with self.assertRaises(ValueError):
                save_complete_report(first, alias, build_report(sample_model()), sample_model())

    def test_shared_target_and_reverse_pair_concurrency_complete_without_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = multiprocessing.get_context("fork")
            event = context.Event()
            shared = Path(tmp) / "shared.md"
            data_one = Path(tmp) / "one.json"
            data_two = Path(tmp) / "two.json"
            processes = [
                context.Process(target=_save_worker, args=(shared, data_one, "marker-one", event)),
                context.Process(target=_save_worker, args=(shared, data_two, "marker-two", event)),
            ]
            for process in processes:
                process.start()
            event.set()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            shared_text = shared.read_text(encoding="utf-8")
            if "marker-one" in shared_text:
                self.assertEqual(json.loads(data_one.read_text(encoding="utf-8"))["overview"]["facts"][0], "marker-one")
            else:
                self.assertIn("marker-two", shared_text)
                self.assertEqual(json.loads(data_two.read_text(encoding="utf-8"))["overview"]["facts"][0], "marker-two")

            event = context.Event()
            left = Path(tmp) / "left.bundle"
            right = Path(tmp) / "right.bundle"
            reverse = [
                context.Process(target=_save_worker, args=(left, right, "forward", event)),
                context.Process(target=_save_worker, args=(right, left, "reverse", event)),
            ]
            for process in reverse:
                process.start()
            event.set()
            for process in reverse:
                process.join(10)
                self.assertEqual(process.exitcode, 0)

    def test_new_target_race_uses_no_clobber_and_preserves_competitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            competitor = b"race-winner"
            real_link = os.link
            calls = {"value": 0}

            def install_competitor_then_link(source, target):
                calls["value"] += 1
                if calls["value"] == 1:
                    report.write_bytes(competitor)
                return real_link(source, target)

            with patch("storage.os.link", side_effect=install_competitor_then_link):
                with self.assertRaises(StorageError):
                    save_complete_report(report, data, build_report(sample_model()), sample_model())
            self.assertEqual(report.read_bytes(), competitor)
            self.assertFalse(data.exists())

    def test_post_commit_directory_fsync_failure_is_not_reported_as_save_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            markdown = build_report(sample_model())
            with patch("storage._fsync_directory", side_effect=OSError("fsync failed")):
                save_complete_report(report, data, markdown, sample_model())
            self.assertEqual(report.read_text(encoding="utf-8"), markdown)
            self.assertEqual(json.loads(data.read_text(encoding="utf-8")), sample_model())

    def test_concurrent_saves_never_create_mixed_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            data = Path(tmp) / "data.json"
            context = multiprocessing.get_context("fork")
            event = context.Event()
            processes = [
                context.Process(target=_save_worker, args=(report, data, marker, event))
                for marker in ("marker-one", "marker-two")
            ]
            for process in processes:
                process.start()
            event.set()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            markdown = report.read_text(encoding="utf-8")
            model = json.loads(data.read_text(encoding="utf-8"))
            marker = model["overview"]["facts"][0]
            self.assertIn(marker, markdown)
            self.assertNotIn("marker-one" if marker == "marker-two" else "marker-two", markdown)


if __name__ == "__main__":
    unittest.main()
