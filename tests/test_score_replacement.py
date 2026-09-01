#!/usr/bin/env python3
"""Tests for reusability scoring and evidence-gated replacement analysis."""

import copy
import json
import math
import sys
import unittest
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from models import RepositoryRecord
from replacement import analyze_replacement
from score import score_reusability


def _repo(full_name, **overrides):
    defaults = {
        "description": "AI agent framework with MCP tool integration",
        "primary_category": "AI",
        "secondary_tags": ["Agent", "MCP", "Python"],
        "stars_7d_external": 100,
        "license": "Apache-2.0",
    }
    defaults.update(overrides)
    return RepositoryRecord(full_name=full_name, **defaults)


class ReusabilityScoreTests(unittest.TestCase):
    def test_hot_repository_without_license_has_license_risk(self):
        repo = _repo("org/hot", total_stars=500000, license="")
        result = score_reusability(
            repo,
            {"maintenance": 1, "docs": 1, "releases": 1,
             "integration": 1, "community": 1, "ci": 1},
        )
        self.assertIn("许可证不明确", result["risks"])
        self.assertEqual(result["components"]["license"], 0)
        self.assertEqual(result["score"], 80)

    def test_uses_declared_weights_and_rounds_half_up(self):
        result = score_reusability(
            _repo("org/tool"),
            {"maintenance": 0.5, "docs": 0.5, "releases": 0.5,
             "integration": 0.5, "community": 0.5, "ci": 0.5},
        )
        self.assertEqual(
            result["components"],
            {"license": 20.0, "maintenance": 10.0, "docs": 7.5,
             "releases": 7.5, "integration": 7.5, "community": 5.0,
             "ci": 2.5},
        )
        self.assertEqual(result["score"], 60)

        half_point = score_reusability(_repo("org/half"), {"ci": 0.1})
        self.assertEqual(half_point["score"], 21)

    def test_decimal_thresholds_do_not_round_through_float(self):
        below_half = score_reusability(
            _repo("org/below-half", license=""),
            {"ci": Decimal("0.4999999999999999999999999999")},
        )
        exact_half = score_reusability(
            _repo("org/exact-half", license=""),
            {"ci": Decimal("0.5")},
        )
        above_half = score_reusability(
            _repo("org/above-half", license=""),
            {"ci": Decimal("0.5000000000000000000000000001")},
        )

        self.assertEqual(below_half["score"], 2)
        self.assertEqual(exact_half["score"], 3)
        self.assertEqual(above_half["score"], 3)
        for result in (below_half, exact_half, above_half):
            exact_values = [
                Decimal(value) for value in result["components_exact"].values()
            ]
            with localcontext() as context:
                context.prec = max(
                    28,
                    max(len(value.as_tuple().digits) for value in exact_values) + 10,
                )
                reconstructed_score = int(
                    sum(exact_values, Decimal("0")).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
            self.assertEqual(result["score"], reconstructed_score)
            json.dumps(result, ensure_ascii=False)

    def test_exact_components_bound_extreme_decimal_exponents(self):
        tiny_signal = Decimal("1e-1000000000")
        tiny_result = score_reusability(
            _repo("org/tiny", license=""), {"ci": tiny_signal}
        )
        with localcontext() as context:
            context.prec = 28
            expected_tiny_component = tiny_signal * Decimal(5)

        tiny_exact = tiny_result["components_exact"]["ci"]
        self.assertLessEqual(len(tiny_exact), 64)
        self.assertEqual(Decimal(tiny_exact), expected_tiny_component)
        self.assertEqual(tiny_result["score"], 0)

        huge_result = score_reusability(
            _repo("org/huge", license=""),
            {"ci": Decimal("1e1000000000")},
        )
        huge_exact = huge_result["components_exact"]["ci"]
        self.assertLessEqual(len(huge_exact), 64)
        self.assertEqual(Decimal(huge_exact), Decimal(5))
        self.assertEqual(huge_result["score"], 5)

    def test_missing_signals_default_to_zero_and_values_are_clipped(self):
        result = score_reusability(
            _repo("org/tool"), {"maintenance": 10 ** 1000, "docs": -1}
        )
        self.assertEqual(result["score"], 40)
        self.assertEqual(result["components"]["maintenance"], 20.0)
        self.assertEqual(result["components"]["docs"], 0.0)
        self.assertEqual(result["components"]["ci"], 0.0)

    def test_archived_repository_has_risk(self):
        result = score_reusability(_repo("org/old", archived=True), {})
        self.assertIn("仓库已归档", result["risks"])

    def test_scoring_does_not_modify_repository_or_signals(self):
        repo = _repo("org/tool")
        signals = {"maintenance": 0.8, "docs": 0.6}
        original_repo = copy.deepcopy(repo)
        original_signals = copy.deepcopy(signals)
        score_reusability(repo, signals)
        self.assertEqual(repo, original_repo)
        self.assertEqual(signals, original_signals)

    def test_rejects_bool_nonfinite_and_wrong_signal_types_without_value_leak(self):
        invalid_values = [True, math.nan, math.inf, -math.inf, "secret-score"]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    score_reusability(_repo("org/tool"), {"maintenance": value})
                self.assertNotIn(str(value), str(caught.exception))

    def test_rejects_unknown_signal_and_non_mapping_inputs(self):
        with self.assertRaises(ValueError):
            score_reusability(_repo("org/tool"), {"mystery": 1})
        with self.assertRaises(ValueError):
            score_reusability(_repo("org/tool"), [])
        with self.assertRaises(ValueError):
            score_reusability("org/tool", {})

    def test_noassertion_license_is_not_clear(self):
        result = score_reusability(_repo("org/tool", license=" NOASSERTION "), {})
        self.assertEqual(result["components"]["license"], 0.0)
        self.assertIn("许可证不明确", result["risks"])

    def test_license_sentinels_are_nfkc_normalized_and_ignore_format_whitespace(self):
        unclear_licenses = (
            "",
            "NOASSERTION",
            "Other",
            "unknown",
            "unlicensed",
            "n/a",
            "ＮＯＡＳＳＥＲＴＩＯＮ",
            "ｎ／ａ",
            "N\u200bOASSERTION",
            "N O A S S E R T I O N",
        )
        for license_name in unclear_licenses:
            with self.subTest(license_name=license_name):
                result = score_reusability(
                    _repo("org/tool", license=license_name), {}
                )
                self.assertEqual(result["components"]["license"], 0.0)
                self.assertIn("许可证不明确", result["risks"])

    def test_invalid_license_types_are_rejected_without_value_leak(self):
        invalid_licenses = (None, True, 42, ["secret-license"])
        for license_value in invalid_licenses:
            with self.subTest(license_value=license_value):
                with self.assertRaises(ValueError) as caught:
                    score_reusability(
                        _repo("org/tool", license=license_value), {}
                    )
                self.assertNotIn("secret-license", str(caught.exception))


class ReplacementTests(unittest.TestCase):
    def test_no_evidence_never_claims_direct_replacement(self):
        result = analyze_replacement(
            _repo("org/old", stars_7d_external=100),
            _repo("org/new", stars_7d_external=5000),
            [],
        )
        self.assertEqual(result["relation"], "部分替代")
        self.assertEqual(result["confidence"], "中")
        self.assertNotEqual(result["relation"], "直接替代")

    def test_allowed_direct_evidence_enables_direct_replacement(self):
        old = _repo("org/old", stars_7d_external=100)
        new = _repo("org/new", stars_7d_external=5000)
        for evidence_type, canonical_type in (
            ("迁移说明", "migration_guide"),
            ("官方对比", "official_comparison"),
            ("Release", "release"),
            ("Issue", "issue"),
            ("社区讨论", "community_discussion"),
        ):
            with self.subTest(evidence_type=evidence_type):
                result = analyze_replacement(
                    old, new,
                    [{"type": evidence_type,
                      "url": "https://example.test/evidence"}],
                )
                self.assertEqual(result["relation"], "直接替代")
                self.assertEqual(result["confidence"], "高")
                self.assertEqual(result["evidence"][0]["type"], canonical_type)

    def test_non_whitelist_evidence_aliases_are_rejected(self):
        aliases = (
            "migration_guide", "migration", "official_comparison",
            "official_compare", "发布说明", "discussion",
            "community_discussion", "README", "docs",
        )
        for evidence_type in aliases:
            with self.subTest(evidence_type=evidence_type):
                with self.assertRaises(ValueError):
                    analyze_replacement(
                        _repo("org/old", stars_7d_external=100),
                        _repo("org/new", stars_7d_external=5000),
                        [{"type": evidence_type}],
                    )

    def test_overlap_tags_are_normalized_unique_and_stably_sorted(self):
        old = _repo("org/old",
                    secondary_tags=[" MCP ", "agent", "Python", "AGENT"])
        new = _repo("org/new", secondary_tags=["python", " Agent ", "mcp"],
                    stars_7d_external=500)
        result = analyze_replacement(old, new, [])
        self.assertEqual(result["overlap_tags"], ["agent", "mcp", "python"])

    def test_external_zero_is_valid_and_does_not_fall_back_to_local(self):
        old = _repo("org/old", stars_7d_external=0,
                    stars_7d_local=10000)
        new = _repo("org/new", stars_7d_external=1, stars_7d_local=0)
        result = analyze_replacement(old, new, [])
        self.assertEqual(result["relation"], "部分替代")

    def test_finite_arbitrary_precision_integer_growth_is_supported(self):
        old = _repo("org/old", stars_7d_external=10 ** 1000)
        new = _repo("org/new", stars_7d_external=10 ** 1001)
        result = analyze_replacement(old, new, [])
        self.assertEqual(result["relation"], "部分替代")

    def test_local_growth_is_used_only_when_external_is_missing(self):
        old = _repo("org/old", stars_7d_external=None, stars_7d_local=300)
        new = _repo("org/new", stars_7d_external=None, stars_7d_local=100)
        result = analyze_replacement(old, new, [])
        self.assertEqual(result["relation"], "同赛道共存")
        self.assertEqual(result["confidence"], "低")

    def test_same_category_and_higher_growth_without_two_tags_is_attention_split(self):
        old = _repo("org/old", secondary_tags=["agent"], stars_7d_external=1)
        new = _repo("org/new", secondary_tags=["agent"], stars_7d_external=2)
        result = analyze_replacement(old, new, [])
        self.assertEqual(result["relation"], "注意力分流")
        self.assertEqual(result["confidence"], "中低")

    def test_same_category_without_higher_growth_is_coexistence(self):
        old = _repo("org/old", stars_7d_external=50)
        new = _repo("org/new", stars_7d_external=50)
        result = analyze_replacement(old, new, [])
        self.assertEqual(result["relation"], "同赛道共存")
        self.assertEqual(result["confidence"], "低")

    def test_different_category_has_no_clear_replacement(self):
        old = _repo("org/old", primary_category="AI", stars_7d_external=1)
        new = _repo("org/project2", primary_category="开发",
                    stars_7d_external=5000)
        result = analyze_replacement(old, new, [])
        self.assertEqual(result["relation"], "无明确替代者")
        self.assertEqual(result["confidence"], "低")

    def test_other_category_never_counts_as_same_category(self):
        for old_category, new_category in (
            (" 其他 ", "其他"),
            (" Other ", "other"),
            ("OTHER", " other "),
        ):
            with self.subTest(category=old_category):
                result = analyze_replacement(
                    _repo("org/old", primary_category=old_category,
                          stars_7d_external=1),
                    _repo("org/new", primary_category=new_category,
                          stars_7d_external=5000),
                    [],
                )
                self.assertEqual(result["relation"], "无明确替代者")

    def test_different_scenario_without_two_overlap_tags_is_attention_split(self):
        old = _repo("org/old", description="database migration utility",
                    secondary_tags=["python", "database"],
                    stars_7d_external=1)
        new = _repo("org/new", description="browser testing library",
                    secondary_tags=["python", "browser"],
                    stars_7d_external=5000)
        result = analyze_replacement(old, new, [])
        self.assertEqual(result["relation"], "注意力分流")

    def test_evidence_types_are_case_and_whitespace_stable(self):
        result = analyze_replacement(
            _repo("org/old", stars_7d_external=1),
            _repo("org/new", stars_7d_external=2),
            [{"type": "  RELEASE  ", "note": "v2 migration"}],
        )
        self.assertEqual(result["relation"], "直接替代")
        self.assertEqual(
            result["evidence"], [{"type": "release", "note": "v2 migration"}]
        )

    def test_chinese_evidence_types_only_allow_exact_text_after_strip(self):
        for evidence_type, canonical_type in (
            ("  迁移说明  ", "migration_guide"),
            ("  官方对比  ", "official_comparison"),
            ("  社区讨论  ", "community_discussion"),
        ):
            with self.subTest(evidence_type=evidence_type):
                result = analyze_replacement(
                    _repo("org/old", stars_7d_external=1),
                    _repo("org/new", stars_7d_external=2),
                    [{"type": evidence_type}],
                )
                self.assertEqual(result["evidence"][0]["type"], canonical_type)

    def test_unicode_lookalikes_cannot_bypass_ascii_evidence_whitelist(self):
        lookalikes = (
            "Ｒｅｌｅａｓｅ",
            "Releaſe",
            "Releаse",
            "Ｉｓｓｕｅ",
            "Ιssue",
        )
        for evidence_type in lookalikes:
            with self.subTest(evidence_type=evidence_type):
                with self.assertRaises(ValueError):
                    analyze_replacement(
                        _repo("org/old", stars_7d_external=1),
                        _repo("org/new", stars_7d_external=2),
                        [{"type": evidence_type}],
                    )

    def test_rejects_non_mapping_and_unknown_evidence_without_value_leak(self):
        invalid = ["secret-evidence", {"type": "secret-evidence"}, {"url": "x"}]
        for evidence in invalid:
            with self.subTest(evidence=evidence):
                with self.assertRaises(ValueError) as caught:
                    analyze_replacement(_repo("org/old"), _repo("org/new"),
                                        [evidence])
                self.assertNotIn("secret-evidence", str(caught.exception))

    def test_rejects_behavior_rewriting_evidence_subclasses_safely(self):
        class StripRewritingString(str):
            def strip(self, chars=None):
                return "Release"

        class GetRewritingDict(dict):
            def get(self, key, default=None):
                if key == "type":
                    return "Release"
                return super().get(key, default)

        invalid_items = (
            {"type": StripRewritingString("secret-evidence")},
            GetRewritingDict({"type": "secret-evidence"}),
        )
        for item in invalid_items:
            with self.subTest(item_type=type(item).__name__):
                with self.assertRaises(ValueError) as caught:
                    analyze_replacement(
                        _repo("org/old", stars_7d_external=1),
                        _repo("org/new", stars_7d_external=2),
                        [item],
                    )
                self.assertNotIn("secret-evidence", str(caught.exception))

    def test_rejects_invalid_growth_values_and_repository_types_safely(self):
        for value in (True, math.nan, math.inf, "secret-growth"):
            with self.subTest(value=value):
                new = _repo("org/new", stars_7d_external=value)
                with self.assertRaises(ValueError) as caught:
                    analyze_replacement(_repo("org/old"), new, [])
                self.assertNotIn(str(value), str(caught.exception))
        with self.assertRaises(ValueError):
            analyze_replacement("org/old", _repo("org/new"), [])

    def test_replacement_analysis_does_not_modify_inputs(self):
        old = _repo("org/old")
        new = _repo("org/new", stars_7d_external=500)
        evidence = [{"type": "issue", "url": "https://example.test/1"}]
        originals = copy.deepcopy((old, new, evidence))
        analyze_replacement(old, new, evidence)
        self.assertEqual((old, new, evidence), originals)


if __name__ == "__main__":
    unittest.main()
