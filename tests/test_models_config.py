#!/usr/bin/env python3
"""Tests for the GitHub trend radar shared models and paths."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from config import (
    DEFAULT_CATEGORY_RULES,
    FIXED_PRIMARY_CATEGORIES,
    RadarPaths,
    validate_fixed_primary_categories,
)
from models import RepositoryRecord


class RepositoryRecordTests(unittest.TestCase):
    def test_normalizes_full_name_and_preserves_secondary_tags_as_a_list(self):
        record = RepositoryRecord(
            full_name=" OpenAI/Codex ", secondary_tags=["Coding Agent"]
        )

        self.assertEqual(record.full_name, "openai/codex")
        self.assertEqual(record.repo_url, "https://github.com/openai/codex")
        self.assertEqual(record.to_dict()["secondary_tags"], ["Coding Agent"])
        self.assertIsInstance(record.to_dict()["secondary_tags"], list)

    def test_rejects_invalid_full_names(self):
        for full_name in (
            "   ",
            "/",
            "a/b/c",
            "owner/ repo",
            "owner /repo",
            "owner/re po",
            "owner/repo/name",
            "owner/.",
            "owner/..",
        ):
            with self.subTest(full_name=full_name):
                with self.assertRaises(ValueError):
                    RepositoryRecord(full_name=full_name)

    def test_accepts_valid_full_names(self):
        for full_name in ("openai/codex", "my-org/repo.name_2"):
            with self.subTest(full_name=full_name):
                record = RepositoryRecord(full_name=full_name)

                self.assertEqual(record.full_name, full_name)

    def test_preserves_explicit_repository_url(self):
        repo_url = "https://example.com/custom/repository"

        record = RepositoryRecord(full_name="Owner/Repo", repo_url=repo_url)

        self.assertEqual(record.repo_url, repo_url)

    def test_default_collections_are_not_shared_between_instances(self):
        first = RepositoryRecord(full_name="owner/first")
        second = RepositoryRecord(full_name="owner/second")

        first.topics.append("ai")
        first.secondary_tags.append("agent")
        first.classification_evidence.append("topic match")
        first.source_records.append({"source": "github"})

        self.assertEqual(second.topics, [])
        self.assertEqual(second.secondary_tags, [])
        self.assertEqual(second.classification_evidence, [])
        self.assertEqual(second.source_records, [])


class RadarPathsTests(unittest.TestCase):
    def test_builds_report_and_archive_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            paths = RadarPaths(workspace, "2026-07-13")

            self.assertEqual(
                paths.latest_report,
                workspace / "最新报告" / "GitHub开源趋势与项目复用雷达-2026-07-13.md",
            )
            self.assertEqual(
                paths.latest_data,
                workspace / "最新报告" / "原始数据-2026-07-13.json",
            )
            self.assertEqual(
                paths.archive_report,
                workspace
                / "历史归档"
                / "2026"
                / "07"
                / "2026-07-13"
                / "GitHub开源趋势与项目复用雷达-2026-07-13.md",
            )
            self.assertEqual(
                paths.archive_data,
                workspace
                / "历史归档"
                / "2026"
                / "07"
                / "2026-07-13"
                / "原始数据-2026-07-13.json",
            )

    def test_builds_configuration_and_history_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            paths = RadarPaths(workspace, "2026-07-13")

            self.assertEqual(paths.watchlist_file, workspace / "配置/项目观察清单.xlsx")
            self.assertEqual(paths.categories_file, workspace / "配置/分类规则.yaml")
            self.assertEqual(
                paths.history_file,
                workspace / "运行状态/历史指标/2026-07.jsonl",
            )
            self.assertEqual(
                paths.history_index, workspace / "运行状态/历史索引.json"
            )
            self.assertEqual(paths.locks_dir, workspace / "运行状态/锁")

    def test_rejects_invalid_run_dates(self):
        for run_date in ("2026-99-88", "2026-07-13-extra", "20260713"):
            with self.subTest(run_date=run_date):
                with self.assertRaises(ValueError):
                    RadarPaths(Path("/tmp/radar"), run_date)


class FixedPrimaryCategoryTests(unittest.TestCase):
    def test_allowlist_is_exactly_the_default_primary_category_keys(self):
        self.assertEqual(
            FIXED_PRIMARY_CATEGORIES,
            tuple(DEFAULT_CATEGORY_RULES["一级分类"].keys()),
        )
        self.assertIn("AI", FIXED_PRIMARY_CATEGORIES)

    def test_rejects_custom_primary_category_but_allows_secondary_changes(self):
        custom_primary = copy.deepcopy(DEFAULT_CATEGORY_RULES)
        custom_primary["一级分类"]["自定义"] = {}
        with self.assertRaises(ValueError):
            validate_fixed_primary_categories(custom_primary)

        custom_override = copy.deepcopy(DEFAULT_CATEGORY_RULES)
        custom_override["人工覆盖"] = {
            "org/tool": {"一级分类": "自定义"}
        }
        with self.assertRaises(ValueError):
            validate_fixed_primary_categories(custom_override)

        secondary_only = copy.deepcopy(DEFAULT_CATEGORY_RULES)
        secondary_only["二级标签"]["自定义二级标签"] = {"topics": ["custom"]}
        self.assertIs(
            validate_fixed_primary_categories(secondary_only),
            secondary_only,
        )


if __name__ == "__main__":
    unittest.main()
