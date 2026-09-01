#!/usr/bin/env python3
"""Tests for repository merging, typing, and configurable classification."""

import itertools
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from classify import _as_terms, classify_repository, detect_repository_type, merge_records
from config import ensure_default_category_rules, load_category_rules
from models import RepositoryRecord


def field_provenance(record):
    return next(
        (item["_field_provenance"]
        for item in record.source_records
        if "_field_provenance" in item),
        None,
    )


class MergeRecordsTests(unittest.TestCase):
    def test_merges_case_insensitive_full_names_without_losing_total_stars(self):
        records = [
            RepositoryRecord(full_name="OpenAI/Codex", total_stars=40000),
            RepositoryRecord(
                full_name="openai/CODEX", stars_7d_external=1200
            ),
        ]

        merged = merge_records(records)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].full_name, "openai/codex")
        self.assertEqual(merged[0].total_stars, 40000)
        self.assertEqual(merged[0].stars_7d_external, 1200)

    def test_merges_independent_metrics_lists_provenance_and_safe_defaults(self):
        first = RepositoryRecord(
            full_name="org/tool",
            description="A concrete description",
            primary_category="AI",
            repository_type="可运行软件",
            secondary_tags=["AI Agent", "MCP"],
            classification_evidence=["topic: ai", "topic: mcp"],
            topics=["ai", "mcp"],
            total_stars=40000,
            stars_24h_external=100,
            stars_24h_local=90,
            stars_7d_local=600,
            growth_acceleration=1.25,
            archived=False,
            source_records=[{"source": "github", "scope": "search"}],
            data_confidence="高",
        )
        second = RepositoryRecord(
            full_name="ORG/TOOL",
            description="",
            primary_category="其他",
            repository_type="其他",
            secondary_tags=["MCP", "RAG"],
            classification_evidence=["topic: mcp", "keyword: rag"],
            topics=["mcp", "rag"],
            stars_7d_external=700,
            stars_30d_external=2100,
            archived=True,
            source_records=[
                {"source": "github", "scope": "search"},
                {"source": "ossinsight", "period": "7d"},
            ],
            data_confidence="低",
        )

        record = merge_records([first, second])[0]

        self.assertEqual(record.total_stars, 40000)
        self.assertEqual(record.stars_24h_external, 100)
        self.assertEqual(record.stars_7d_external, 700)
        self.assertEqual(record.stars_30d_external, 2100)
        self.assertEqual(record.stars_24h_local, 90)
        self.assertEqual(record.stars_7d_local, 600)
        self.assertEqual(record.growth_acceleration, 1.25)
        self.assertEqual(record.topics, ["ai", "mcp", "rag"])
        self.assertEqual(record.secondary_tags, ["AI Agent", "MCP", "RAG"])
        self.assertEqual(
            record.classification_evidence,
            ["topic: ai", "topic: mcp", "keyword: rag"],
        )
        self.assertEqual(
            record.source_records[:2],
            [
                {"source": "github", "scope": "search"},
                {"source": "ossinsight", "period": "7d"},
            ],
        )
        self.assertIsNotNone(field_provenance(record))
        self.assertTrue(record.archived)
        self.assertEqual(record.description, "A concrete description")
        self.assertEqual(record.primary_category, "AI")
        self.assertEqual(record.repository_type, "可运行软件")
        self.assertEqual(record.data_confidence, "高")

    def test_source_authority_and_conflicts_are_independent_of_input_order(self):
        github = RepositoryRecord(
            full_name="org/tool",
            description="Official GitHub description",
            total_stars=40000,
            stars_7d_local=650,
            license="MIT",
            topics=["ai", "cli"],
            pushed_at="2026-07-10T00:00:00Z",
            source_records=[{"source": "github", "scope": "repo_api"}],
        )
        ossinsight = RepositoryRecord(
            full_name="ORG/TOOL",
            description="OSSInsight description",
            total_stars=50000,
            stars_7d_external=1200,
            topics=["llm"],
            pushed_at="2026-07-12T00:00:00Z",
            source_records=[{"source": "ossinsight", "period": "7d"}],
        )
        trending = RepositoryRecord(
            full_name="org/tool",
            description="Stale Trending description",
            total_stars=60000,
            stars_7d_external=900,
            stars_24h_local=88,
            license="Unknown",
            topics=["trending"],
            pushed_at="2026-07-13T00:00:00Z",
            source_records=[{"source": "github_trending", "period": "weekly"}],
        )

        forward = merge_records([github, ossinsight, trending])[0]
        reverse = merge_records([trending, ossinsight, github])[0]

        self.assertEqual(forward.to_dict(), reverse.to_dict())
        self.assertEqual(forward.total_stars, 40000)
        self.assertEqual(forward.description, "Official GitHub description")
        self.assertEqual(forward.license, "MIT")
        self.assertEqual(forward.stars_7d_external, 1200)
        self.assertEqual(forward.stars_7d_local, 650)
        self.assertEqual(forward.stars_24h_local, 88)
        self.assertEqual(forward.topics, ["ai", "cli", "llm", "trending"])
        self.assertEqual(
            forward.source_records[:3],
            [
                {"source": "github", "scope": "repo_api"},
                {"source": "ossinsight", "period": "7d"},
                {"source": "github_trending", "period": "weekly"},
            ],
        )
        self.assertIsNotNone(field_provenance(forward))

    def test_same_source_uses_numeric_max_and_latest_then_lexical_string(self):
        old = RepositoryRecord(
            full_name="org/tool",
            total_stars=500,
            description="Old description",
            pushed_at="2026-07-10T00:00:00Z",
            source_records=[{"source": "github", "scope": "search"}],
        )
        latest_a = RepositoryRecord(
            full_name="org/tool",
            total_stars=400,
            description="Alpha latest",
            pushed_at="2026-07-12T00:00:00Z",
            source_records=[{"source": "github", "scope": "repo_api-a"}],
        )
        latest_z = RepositoryRecord(
            full_name="org/tool",
            total_stars=450,
            description="Zulu latest",
            pushed_at="2026-07-12T00:00:00Z",
            source_records=[{"source": "github", "scope": "repo_api-z"}],
        )

        forward = merge_records([old, latest_a, latest_z])[0]
        reverse = merge_records([latest_z, latest_a, old])[0]

        self.assertEqual(forward.to_dict(), reverse.to_dict())
        self.assertEqual(forward.total_stars, 500)
        self.assertEqual(forward.description, "Zulu latest")

    def test_merge_result_and_nested_values_are_deep_copies(self):
        source_record = {"source": "github", "details": {"scope": "search"}}
        original = RepositoryRecord(
            full_name="org/tool", topics=["ai"], source_records=[source_record]
        )

        merged = merge_records([original])[0]
        merged.topics.append("mcp")
        merged.source_records[0]["details"]["scope"] = "changed"
        original.description = "changed later"

        self.assertEqual(original.topics, ["ai"])
        self.assertEqual(source_record["details"]["scope"], "search")
        self.assertEqual(merged.description, "")

    def test_merge_is_associative_with_field_level_external_metric_provenance(self):
        github = RepositoryRecord(
            full_name="org/tool",
            total_stars=40000,
            description="Official metadata",
            source_records=[{"source": "github", "scope": "repo_api"}],
        )
        oss_100 = RepositoryRecord(
            full_name="org/tool",
            stars_7d_external=100,
            source_records=[{"source": "ossinsight", "period": "7d", "run": 1}],
        )
        oss_200 = RepositoryRecord(
            full_name="org/tool",
            stars_7d_external=200,
            source_records=[{"source": "ossinsight", "period": "7d", "run": 2}],
        )

        flat = merge_records([github, oss_100, oss_200])[0]
        staged = merge_records([merge_records([github, oss_100])[0], oss_200])[0]

        self.assertEqual(flat.to_dict(), staged.to_dict())
        self.assertEqual(flat.stars_7d_external, 200)
        provenance = field_provenance(flat)
        self.assertIsNotNone(provenance)
        self.assertEqual(provenance["stars_7d_external"]["source"], "ossinsight")

    def test_merge_is_idempotent_and_invariant_under_input_permutations(self):
        records = [
            RepositoryRecord(
                full_name="org/tool",
                total_stars=10,
                topics=["ai"],
                source_records=[{"source": "github", "scope": "repo_api"}],
            ),
            RepositoryRecord(
                full_name="org/tool",
                stars_7d_external=20,
                topics=["llm"],
                source_records=[{"source": "ossinsight", "period": "7d"}],
            ),
            RepositoryRecord(
                full_name="org/tool",
                stars_7d_external=15,
                topics=["trending"],
                source_records=[{"source": "github_trending", "period": "weekly"}],
            ),
        ]

        expected = merge_records(records)[0]
        for permutation in itertools.permutations(records):
            with self.subTest(order=[item.source_records[0]["source"] for item in permutation]):
                self.assertEqual(
                    merge_records(permutation)[0].to_dict(), expected.to_dict()
                )
        self.assertEqual(merge_records([expected])[0].to_dict(), expected.to_dict())
        provenance = field_provenance(expected)
        self.assertIsNotNone(provenance)
        self.assertIn("topics", provenance)

    def test_default_strings_survive_when_they_are_the_only_nonempty_values(self):
        single = RepositoryRecord(
            full_name="org/single",
            repository_type="其他",
            primary_category="其他",
            data_confidence="低",
            source_records=[{"source": "github"}],
        )
        first = RepositoryRecord(
            full_name="org/multiple",
            primary_category="其他",
            data_confidence="低",
            source_records=[{"source": "github"}],
        )
        second = RepositoryRecord(
            full_name="org/multiple",
            primary_category="其他",
            data_confidence="低",
            source_records=[{"source": "ossinsight"}],
        )

        single_result = merge_records([single])[0]
        multiple_result = merge_records([first, second])[0]

        self.assertEqual(single_result.repository_type, "其他")
        self.assertEqual(single_result.primary_category, "其他")
        self.assertEqual(single_result.data_confidence, "低")
        self.assertEqual(multiple_result.primary_category, "其他")
        self.assertEqual(multiple_result.data_confidence, "低")

    def test_specific_strings_replace_default_strings(self):
        default = RepositoryRecord(
            full_name="org/tool",
            primary_category="其他",
            data_confidence="低",
            source_records=[{"source": "github"}],
        )
        specific = RepositoryRecord(
            full_name="org/tool",
            primary_category="AI",
            data_confidence="高",
            source_records=[{"source": "ossinsight"}],
        )

        result = merge_records([default, specific])[0]

        self.assertEqual(result.primary_category, "AI")
        self.assertEqual(result.data_confidence, "高")


class RepositoryTypeTests(unittest.TestCase):
    def test_detects_repository_types_with_reference_material_precedence(self):
        cases = (
            ("org/awesome-ai", "A curated awesome collection", ["awesome-list"], "Awesome 清单"),
            ("org/learn-ai", "An AI tutorial and course", ["dataset"], "教程或课程"),
            ("org/ai-handbook", "A practical handbook", ["model"], "书籍或资料"),
            ("org/corpus", "A speech dataset", [], "数据集"),
            ("org/weights", "Foundation model weights", [], "模型"),
            ("org/starter", "Project template", [], "模板"),
            ("org/client", "Python SDK and library", [], "SDK 或库"),
            ("org/web", "Self-hosted productivity application", [], "可运行软件"),
        )

        for full_name, description, topics, expected in cases:
            with self.subTest(full_name=full_name):
                record = RepositoryRecord(
                    full_name=full_name, description=description, topics=topics
                )
                self.assertEqual(detect_repository_type(record), expected)

    def test_does_not_match_keywords_as_arbitrary_substrings(self):
        record = RepositoryRecord(
            full_name="org/remodel", description="A modern application"
        )

        self.assertEqual(detect_repository_type(record), "可运行软件")

    def test_description_phrases_avoid_software_false_positives(self):
        for description in (
            "Course scheduling application",
            "A data model tool",
            "A template editor for teams",
        ):
            with self.subTest(description=description):
                record = RepositoryRecord(
                    full_name="org/product", description=description
                )
                self.assertEqual(detect_repository_type(record), "可运行软件")

    def test_explicit_name_topic_and_description_patterns_are_detected(self):
        cases = (
            ("org/awesome-agents", "Resources", [], "Awesome 清单"),
            ("org/resource", "A tutorial for building agents", [], "教程或课程"),
            ("org/resource", "Online course materials", [], "教程或课程"),
            ("org/resource", "A practical guide for operators", [], "书籍或资料"),
            ("org/resource", "A concise guide", [], "书籍或资料"),
            ("org/resource", "An engineering handbook", [], "书籍或资料"),
            ("org/resource", "A speech dataset", [], "数据集"),
            ("org/resource", "Pretrained model weights", [], "模型"),
            ("org/resource", "A repository template", [], "模板"),
            ("org/resource", "Official Python SDK", [], "SDK 或库"),
            ("org/resource", "Official client SDKs", [], "SDK 或库"),
            ("org/resource", "A reusable library", [], "SDK 或库"),
            ("org/resource", "A web framework", [], "SDK 或库"),
            ("org/resource", "Modular frameworks", [], "SDK 或库"),
            ("org/resource", "A developer toolkit", [], "SDK 或库"),
            ("org/resource", "Application", ["dataset"], "数据集"),
        )
        for full_name, description, topics, expected in cases:
            with self.subTest(description=description, topics=topics):
                record = RepositoryRecord(
                    full_name=full_name, description=description, topics=topics
                )
                self.assertEqual(detect_repository_type(record), expected)


class ClassifyRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.rules = {
            "一级分类": {
                "AI": {
                    "ossinsight_collections": ["AI Native"],
                    "topics": ["llm"],
                    "keywords": ["artificial intelligence"],
                },
                "开发工具": {
                    "ossinsight_collections": ["Developer Tools"],
                    "topics": ["developer-tools"],
                    "keywords": ["coding assistant"],
                },
                "安全": {"topics": ["security"], "keywords": ["security"]},
                "其他": {},
            },
            "二级标签": {
                "AI Agent": {"topics": ["ai-agent"], "keywords": ["agent"]},
                "MCP": {
                    "topics": ["mcp"],
                    "keywords": ["model context protocol"],
                },
                "CLI": {"topics": ["cli"], "keywords": ["command line"]},
            },
            "人工覆盖": {},
        }

    def test_manual_override_has_priority_over_all_automatic_rules(self):
        self.rules["人工覆盖"]["openai/codex"] = {
            "一级分类": "安全",
            "二级标签": ["CLI"],
        }
        record = RepositoryRecord(
            full_name="OpenAI/Codex",
            description="artificial intelligence coding assistant",
            topics=["llm", "mcp"],
        )

        classified = classify_repository(
            record, self.rules, collection_names=["AI Native"]
        )

        self.assertEqual(classified.primary_category, "安全")
        self.assertEqual(classified.secondary_tags, ["CLI"])
        self.assertTrue(
            any(
                "人工覆盖" in evidence and "openai/codex" in evidence
                for evidence in classified.classification_evidence
            )
        )

    def test_primary_category_priority_and_evidence_identify_exact_source(self):
        cases = (
            (
                ["AI Native"],
                ["developer-tools"],
                "coding assistant",
                "AI",
                "OSSInsight collection_names",
                "AI Native",
            ),
            (
                [],
                ["developer-tools"],
                "artificial intelligence",
                "开发工具",
                "GitHub topics",
                "developer-tools",
            ),
            (
                [],
                [],
                "An artificial intelligence project",
                "AI",
                "关键词",
                "artificial intelligence",
            ),
        )

        for collections, topics, description, category, source, matched in cases:
            with self.subTest(source=source):
                record = RepositoryRecord(
                    full_name="org/tool", description=description, topics=topics
                )
                classified = classify_repository(
                    record, self.rules, collection_names=collections
                )
                self.assertEqual(classified.primary_category, category)
                self.assertTrue(
                    any(
                        source in evidence and matched in evidence
                        for evidence in classified.classification_evidence
                    )
                )

    def test_secondary_tags_match_independently_and_are_deduplicated(self):
        record = RepositoryRecord(
            full_name="org/agent",
            description="An agent using the model context protocol",
            topics=["ai-agent", "mcp"],
            secondary_tags=["MCP"],
        )

        classified = classify_repository(record, self.rules)

        self.assertEqual(classified.secondary_tags, ["AI Agent", "MCP"])
        self.assertEqual(classified.secondary_tags.count("MCP"), 1)
        self.assertTrue(
            any(
                "二级标签" in evidence and "GitHub topics" in evidence
                for evidence in classified.classification_evidence
            )
        )

    def test_reclassification_clears_stale_category_tags_and_evidence(self):
        record = RepositoryRecord(
            full_name="org/tool",
            description="artificial intelligence model context protocol",
            topics=["llm", "mcp"],
        )
        classify_repository(record, self.rules)
        replacement_rules = {
            "一级分类": {
                "安全": {"topics": ["security"]},
                "其他": {},
            },
            "二级标签": {"CLI": {"topics": ["cli"]}},
            "人工覆盖": {},
        }

        classify_repository(record, replacement_rules)

        self.assertEqual(record.primary_category, "其他")
        self.assertEqual(record.secondary_tags, [])
        self.assertEqual(record.classification_evidence, [])

    def test_empty_manual_override_is_rejected_instead_of_falling_back(self):
        self.rules["人工覆盖"]["org/tool"] = {}
        record = RepositoryRecord(full_name="org/tool", topics=["llm"])

        with self.assertRaises(ValueError):
            classify_repository(record, self.rules)

    def test_direct_rules_reject_nondeterministic_term_types(self):
        invalid_rules = {
            "一级分类": {"AI": {"topics": {"ai"}}, "其他": {}},
            "二级标签": {},
            "人工覆盖": {},
        }

        with self.assertRaises(ValueError):
            classify_repository(
                RepositoryRecord(full_name="org/tool"), invalid_rules
            )

    def test_as_terms_accepts_only_string_or_list_of_nonempty_strings(self):
        self.assertEqual(_as_terms("ai"), ["ai"])
        self.assertEqual(_as_terms(["ai", "mcp"]), ["ai", "mcp"])
        for value in (None, 1, {"ai"}, ("ai",), ["ai", 1], [""]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _as_terms(value)

    def test_direct_classification_uses_the_full_schema_validator(self):
        invalid_rules = (
            {
                **self.rules,
                "人工覆写": {},
            },
            {
                **self.rules,
                "人工覆盖": {
                    "org/tool": {
                        "一级分类": "AI",
                        "二级标签": [],
                        "二级标签讯": [],
                    }
                },
            },
        )
        for rules in invalid_rules:
            with self.subTest(keys=list(rules)):
                with self.assertRaises(ValueError):
                    classify_repository(
                        RepositoryRecord(full_name="org/tool"), rules
                    )


class CategoryRulesTests(unittest.TestCase):
    def test_load_category_rules_reads_valid_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.yaml"
            expected = {"一级分类": {"AI": {}}, "二级标签": {"MCP": {}}, "人工覆盖": {}}
            path.write_text(
                yaml.safe_dump(expected, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            self.assertEqual(load_category_rules(path), expected)

    def test_load_category_rules_rejects_non_mapping_or_missing_sections(self):
        invalid_documents = (
            [],
            {"一级分类": {}},
            {"二级标签": {}},
            {"一级分类": [], "二级标签": {}},
            {"一级分类": {}, "二级标签": []},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.yaml"
            for document in invalid_documents:
                with self.subTest(document=document):
                    path.write_text(
                        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
                    )
                    with self.assertRaises(ValueError):
                        load_category_rules(path)

    def test_load_category_rules_validates_nested_rule_and_override_schema(self):
        invalid_documents = (
            {
                "一级分类": {"AI": []},
                "二级标签": {},
                "人工覆盖": {},
            },
            {
                "一级分类": {"AI": {"topics": [1]}},
                "二级标签": {},
                "人工覆盖": {},
            },
            {
                "一级分类": {},
                "二级标签": {"MCP": {"topics": "mcp"}, "": {}},
                "人工覆盖": {},
            },
            {
                "一级分类": {},
                "二级标签": {},
                "人工覆盖": [],
            },
            {
                "一级分类": {},
                "二级标签": {},
                "人工覆盖": {"org/tool": {}},
            },
            {
                "一级分类": {},
                "二级标签": {},
                "人工覆盖": {
                    "org/tool": {"一级分类": "", "二级标签": []}
                },
            },
            {
                "一级分类": {},
                "二级标签": {},
                "人工覆盖": {
                    "org/tool": {"一级分类": "AI", "二级标签": "MCP"}
                },
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.yaml"
            for document in invalid_documents:
                with self.subTest(document=document):
                    path.write_text(
                        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
                    )
                    with self.assertRaises(ValueError) as raised:
                        load_category_rules(path)
                    self.assertIn(str(path), str(raised.exception))

    def test_load_category_rules_rejects_unknown_keys_and_invalid_override_repo(self):
        invalid_documents = (
            {
                "一级分类": {},
                "二级标签": {},
                "人工覆盖": {},
                "人工覆写": {},
            },
            {
                "一级分类": {},
                "二级标签": {},
                "人工覆盖": {
                    "org/tool": {
                        "一级分类": "AI",
                        "二级标签讯": [],
                    }
                },
            },
            {
                "一级分类": {},
                "二级标签": {},
                "人工覆盖": {
                    "not-a-repo": {"一级分类": "AI"}
                },
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.yaml"
            for document in invalid_documents:
                with self.subTest(document=document):
                    path.write_text(
                        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
                    )
                    with self.assertRaises(ValueError) as raised:
                        load_category_rules(path)
                    self.assertIn(str(path), str(raised.exception))

    def test_load_category_rules_wraps_yaml_io_and_encoding_errors_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            syntax_path = root / "syntax.yaml"
            syntax_path.write_text("一级分类: [\nSECRET_VALUE", encoding="utf-8")
            with self.assertRaises(ValueError) as syntax_error:
                load_category_rules(syntax_path)
            syntax_message = str(syntax_error.exception)
            self.assertIn(str(syntax_path), syntax_message)
            self.assertIn("line", syntax_message)
            self.assertIn("column", syntax_message)
            self.assertNotIn("SECRET_VALUE", syntax_message)

            unsafe_path = root / "unsafe.yaml"
            unsafe_path.write_text(
                "!!python/object/apply:os.system ['SECRET_COMMAND']", encoding="utf-8"
            )
            with self.assertRaises(ValueError) as unsafe_error:
                load_category_rules(unsafe_path)
            self.assertIn(str(unsafe_path), str(unsafe_error.exception))
            self.assertNotIn("SECRET_COMMAND", str(unsafe_error.exception))

            missing_path = root / "missing.yaml"
            with self.assertRaises(ValueError) as missing_error:
                load_category_rules(missing_path)
            self.assertIn(str(missing_path), str(missing_error.exception))

            unicode_path = root / "unicode.yaml"
            unicode_path.write_bytes(b"\xff\xfe\xfa")
            with self.assertRaises(ValueError) as unicode_error:
                load_category_rules(unicode_path)
            self.assertIn(str(unicode_path), str(unicode_error.exception))

            io_path = root / "io.yaml"
            with patch("config.Path.read_text", side_effect=OSError("SECRET_IO")):
                with self.assertRaises(ValueError) as io_error:
                    load_category_rules(io_path)
            self.assertIn(str(io_path), str(io_error.exception))
            self.assertNotIn("SECRET_IO", str(io_error.exception))

    def test_ensure_default_rules_writes_chinese_defaults_once(self):
        expected_primary = [
            "AI",
            "开发工具",
            "基础设施与 DevOps",
            "数据与数据库",
            "安全",
            "前端与 UI",
            "移动端",
            "其他",
        ]
        expected_secondary = [
            "AI Agent",
            "Coding Agent",
            "MCP",
            "RAG",
            "Agent Memory",
            "模型推理",
            "多模态",
            "TTS",
            "AI 视频",
            "CLI",
            "IDE",
            "CI/CD",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "分类规则.yaml"

            rules = ensure_default_category_rules(path)

            self.assertTrue(path.exists())
            self.assertEqual(list(rules["一级分类"]), expected_primary)
            self.assertEqual(list(rules["二级标签"]), expected_secondary)
            self.assertEqual(rules["人工覆盖"], {})
            self.assertIn("一级分类", path.read_text(encoding="utf-8"))

            sentinel = {
                "一级分类": {"sentinel": {}},
                "二级标签": {"sentinel-tag": {}},
                "人工覆盖": {},
            }
            sentinel_text = yaml.safe_dump(
                sentinel, allow_unicode=True, sort_keys=False
            )
            path.write_text(sentinel_text, encoding="utf-8")

            self.assertEqual(ensure_default_category_rules(path), sentinel)
            self.assertEqual(path.read_text(encoding="utf-8"), sentinel_text)

    def test_ensure_default_rules_preserves_concurrently_created_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "配置" / "分类规则.yaml"
            sentinel = {
                "一级分类": {"sentinel": {}},
                "二级标签": {"sentinel-tag": {}},
                "人工覆盖": {},
            }
            sentinel_text = yaml.safe_dump(
                sentinel, allow_unicode=True, sort_keys=False
            )

            def concurrent_publish(_temporary, target):
                Path(target).write_text(sentinel_text, encoding="utf-8")
                raise FileExistsError

            with patch("os.link", side_effect=concurrent_publish):
                self.assertEqual(ensure_default_category_rules(path), sentinel)

            self.assertEqual(path.read_text(encoding="utf-8"), sentinel_text)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_ensure_default_rules_cleans_temporary_file_on_write_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "配置" / "分类规则.yaml"

            with patch("os.fsync", side_effect=OSError("interrupted")):
                with self.assertRaises(ValueError):
                    ensure_default_category_rules(path)

            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
