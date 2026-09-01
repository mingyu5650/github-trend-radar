#!/usr/bin/env python3
"""Tests for local history snapshots and cooling detection."""

import copy
import json
import multiprocessing
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from history import (
    HistoryError,
    calculate_growth_acceleration,
    calculate_local_growth,
    detect_cooling,
    load_history_rows,
    parse_time,
    upsert_history_rows,
)


def _concurrent_upsert(path, start_event, index):
    """Process target kept at module scope for multiprocessing compatibility."""
    start_event.wait()
    upsert_history_rows(
        path,
        "2026-07-13",
        [{"repo": f"org/repo-{index}", "stars": index, "label": "并发"}],
    )


class ParseTimeTests(unittest.TestCase):
    def test_accepts_z_and_aware_datetime(self):
        self.assertEqual(
            parse_time("2026-07-13T08:00:00Z"),
            datetime(2026, 7, 13, 8, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_time(datetime(2026, 7, 13, 8, tzinfo=timezone.utc)),
            datetime(2026, 7, 13, 8, tzinfo=timezone.utc),
        )

    def test_rejects_naive_and_invalid_values_without_echoing_input(self):
        for value in ("2026-07-13T08:00:00", "secret-invalid-time"):
            with self.subTest(value=value):
                with self.assertRaises(HistoryError) as caught:
                    parse_time(value)
                self.assertNotIn(value, str(caught.exception))


class LocalGrowthTests(unittest.TestCase):
    def test_24_hour_growth_uses_two_points(self):
        rows = [
            {"at": "2026-07-12T08:00:00Z", "stars": 100},
            {"at": "2026-07-13T08:00:00Z", "stars": 140},
        ]

        self.assertEqual(calculate_local_growth(rows, 1), 40)

    def test_returns_none_for_eight_hour_gap_or_fewer_than_two_points(self):
        short_gap = [
            {"at": "2026-07-13T00:00:00Z", "stars": 100},
            {"at": "2026-07-13T08:00:00Z", "stars": 140},
        ]

        self.assertIsNone(calculate_local_growth(short_gap, 1))
        self.assertIsNone(calculate_local_growth(short_gap[:1], 1))

    def test_seven_day_window_is_inclusive_from_144_to_192_hours(self):
        latest = "2026-07-13T08:00:00Z"
        for baseline, expected in (
            ("2026-07-07T08:00:00Z", 40),
            ("2026-07-05T08:00:00Z", 30),
        ):
            with self.subTest(baseline=baseline):
                rows = [
                    {"at": baseline, "stars": 100},
                    {"at": latest, "stars": 140 if expected == 40 else 130},
                ]
                self.assertEqual(calculate_local_growth(rows, 7), expected)

        outside = [
            {"at": "2026-07-07T09:00:00Z", "stars": 100},
            {"at": latest, "stars": 140},
        ]
        self.assertIsNone(calculate_local_growth(outside, 7))

    def test_sorts_unsorted_input_and_chooses_candidate_closest_to_target(self):
        rows = [
            {"at": "2026-07-13T08:00:00Z", "stars": 150},
            {"at": "2026-07-12T09:00:00Z", "stars": 105},
            {"at": "2026-07-12T08:00:00Z", "stars": 100},
            {"at": "2026-07-12T07:00:00Z", "stars": 90},
        ]
        original = copy.deepcopy(rows)

        self.assertEqual(calculate_local_growth(rows, 1), 50)
        self.assertEqual(rows, original)

    def test_seven_day_candidate_closest_to_168_hours_wins(self):
        rows = [
            {"at": "2026-07-06T08:00:00Z", "stars": 100},
            {"at": "2026-07-06T20:00:00Z", "stars": 120},
            {"at": "2026-07-13T08:00:00Z", "stars": 170},
        ]

        self.assertEqual(calculate_local_growth(rows, 7), 70)

    def test_rejects_mixed_naive_aware_and_invalid_stars_safely(self):
        mixed = [
            {"at": "2026-07-12T08:00:00", "stars": 100},
            {"at": "2026-07-13T08:00:00Z", "stars": 140},
        ]
        with self.assertRaises(HistoryError):
            calculate_local_growth(mixed, 1)

        with self.assertRaises(HistoryError) as caught:
            calculate_local_growth(
                [
                    {"at": "2026-07-12T08:00:00Z", "stars": "secret-stars"},
                    {"at": "2026-07-13T08:00:00Z", "stars": 140},
                ],
                1,
            )
        self.assertNotIn("secret-stars", str(caught.exception))


class GrowthAccelerationTests(unittest.TestCase):
    def test_calculates_daily_growth_relative_to_weekly_daily_average(self):
        self.assertEqual(calculate_growth_acceleration(40, 140), 2.0)
        self.assertEqual(calculate_growth_acceleration(-10, 140), -0.5)

    def test_returns_none_for_missing_or_nonpositive_week_growth(self):
        for day, week in ((None, 70), (10, None), (10, 0), (10, -70)):
            with self.subTest(day=day, week=week):
                self.assertIsNone(calculate_growth_acceleration(day, week))


class CoolingDetectionTests(unittest.TestCase):
    def test_detects_rank_and_seventy_percent_growth_drop(self):
        result = detect_cooling(
            {"org/tool": 20},
            {"org/tool": 51},
            {"org/tool": (100, 30)},
        )

        self.assertEqual(
            result,
            [
                {
                    "repo": "org/tool",
                    "reason_code": "rank_and_growth_drop",
                    "previous_rank": 20,
                    "current_rank": 51,
                    "growth_drop_ratio": 0.7,
                }
            ],
        )

    def test_detects_falling_out_of_top50_without_growth_drop(self):
        result = detect_cooling(
            {"org/tool": 20},
            {"org/tool": 51},
            {"org/tool": (100, 90)},
        )

        self.assertEqual(
            result,
            [
                {
                    "repo": "org/tool",
                    "reason_code": "fell_out_of_top50",
                    "previous_rank": 20,
                    "current_rank": 51,
                }
            ],
        )

    def test_detects_top20_repo_missing_from_current_as_fallen_out(self):
        result = detect_cooling(
            {"org/tool": 20},
            {},
            {"org/tool": (100, 90)},
        )

        self.assertEqual(
            result,
            [
                {
                    "repo": "org/tool",
                    "reason_code": "fell_out_of_top50",
                    "previous_rank": 20,
                    "current_rank": None,
                }
            ],
        )

    def test_detects_seventy_percent_growth_drop_without_rank_drop(self):
        result = detect_cooling(
            {"org/tool": 30},
            {"org/tool": 40},
            {"org/tool": (100, 30)},
        )

        self.assertEqual(
            result,
            [
                {
                    "repo": "org/tool",
                    "reason_code": "growth_drop_70pct",
                    "growth_drop_ratio": 0.7,
                }
            ],
        )

    def test_detects_category_drop_without_consecutive_slowdown(self):
        result = detect_cooling(
            {"org/tool": 8},
            {"org/tool": 10},
            {"org/tool": (100, 90)},
            category_rank_drops={"org/tool": 11},
        )

        self.assertEqual([item["reason_code"] for item in result], ["category_rank_drop"])

    def test_detects_consecutive_slowdown_without_category_drop(self):
        result = detect_cooling(
            {"org/tool": 8},
            {"org/tool": 10},
            {"org/tool": (100, 90)},
            consecutive_slowdown={"org/tool": 2},
        )

        self.assertEqual([item["reason_code"] for item in result], ["consecutive_slowdown"])

    def test_watchlist_repo_missing_from_current_is_detected_without_previous_rank(self):
        result = detect_cooling(
            {},
            {},
            {},
            watchlist=["Watch/Unicode-项目"],
        )

        self.assertEqual(
            result,
            [{"repo": "watch/unicode-项目", "reason_code": "watchlist_missing"}],
        )

    def test_returns_empty_when_no_rule_triggers(self):
        self.assertEqual(
            detect_cooling(
                {"org/tool": 21},
                {"org/tool": 50},
                {"org/tool": (100, 31)},
                category_rank_drops={"org/tool": 10},
                consecutive_slowdown={"org/tool": 1},
                watchlist=["org/tool"],
            ),
            [],
        )


class HistoryPersistenceTests(unittest.TestCase):
    def test_upsert_updates_same_date_repo_preserves_other_dates_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-07.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"date": "2026-07-12", "repo": "org/old", "stars": 5}
                        ),
                        json.dumps(
                            {"date": "2026-07-13", "repo": "org/tool", "stars": 10}
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            incoming = [
                {"repo": "ORG/TOOL", "stars": 20, "label": "中文"},
                {"repo": "org/another", "stars": 8},
            ]
            original = copy.deepcopy(incoming)

            first = upsert_history_rows(path, "2026-07-13", incoming)
            first_bytes = path.read_bytes()
            second = upsert_history_rows(path, "2026-07-13", incoming)

            self.assertEqual(incoming, original)
            self.assertEqual(first, second)
            self.assertEqual(first_bytes, path.read_bytes())
            self.assertEqual(
                [(row["date"], row["repo"], row["stars"]) for row in second],
                [
                    ("2026-07-12", "org/old", 5),
                    ("2026-07-13", "org/another", 8),
                    ("2026-07-13", "org/tool", 20),
                ],
            )
            self.assertIn("中文", path.read_text(encoding="utf-8"))

    def test_rejects_bad_date_repo_and_stars_without_changing_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            rows = [{"repo": "not-a-repo", "stars": True}]
            original = copy.deepcopy(rows)
            with self.assertRaises(HistoryError):
                upsert_history_rows(path, "2026-99-99", rows)
            self.assertEqual(rows, original)
            self.assertFalse(path.exists())

    def test_existing_duplicate_keeps_physically_last_row_when_at_runs_backwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            rows = [
                {
                    "date": "2026-07-12",
                    "repo": "org/tool",
                    "stars": 100,
                    "at": "2026-07-12T09:00:00Z",
                },
                {
                    "date": "2026-07-12",
                    "repo": "org/tool",
                    "stars": 200,
                    "at": "2026-07-12T08:00:00Z",
                },
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )

            result = upsert_history_rows(
                path, "2026-07-13", [{"repo": "org/new", "stars": 1}]
            )

            existing = next(row for row in result if row["repo"] == "org/tool")
            self.assertEqual(existing["stars"], 200)
            self.assertEqual(existing["at"], "2026-07-12T08:00:00Z")

    def test_existing_duplicate_keeps_physically_last_cross_timezone_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            rows = [
                {
                    "date": "2026-07-12",
                    "repo": "org/tool",
                    "stars": 100,
                    "at": "2026-07-12T10:00:00+08:00",
                },
                {
                    "date": "2026-07-12",
                    "repo": "org/tool",
                    "stars": 200,
                    "at": "2026-07-12T03:00:00Z",
                },
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )

            result = upsert_history_rows(
                path, "2026-07-13", [{"repo": "org/new", "stars": 1}]
            )

            existing = next(row for row in result if row["repo"] == "org/tool")
            self.assertEqual(existing["stars"], 200)
            self.assertEqual(existing["at"], "2026-07-12T03:00:00Z")

    def test_incoming_duplicate_keeps_last_list_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"

            result = upsert_history_rows(
                path,
                "2026-07-13",
                [
                    {"repo": "org/tool", "stars": 100, "label": "first"},
                    {"repo": "org/tool", "stars": 200, "label": "last"},
                ],
            )

            self.assertEqual(
                result,
                [
                    {
                        "date": "2026-07-13",
                        "repo": "org/tool",
                        "stars": 200,
                        "label": "last",
                    }
                ],
            )

    def test_replace_failure_preserves_original_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            original = b'{"date":"2026-07-12","repo":"org/tool","stars":1}\n'
            path.write_bytes(original)

            with patch("history.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(HistoryError):
                    upsert_history_rows(
                        path,
                        "2026-07-13",
                        [{"repo": "org/tool", "stars": 2}],
                    )

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_temp_write_failure_preserves_original_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            original = b'{"date":"2026-07-12","repo":"org/tool","stars":1}\n'
            path.write_bytes(original)

            with patch("history.json.dumps", side_effect=OSError("write failed")):
                with self.assertRaises(HistoryError):
                    upsert_history_rows(
                        path,
                        "2026-07-13",
                        [{"repo": "org/tool", "stars": 2}],
                    )

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_concurrent_upserts_do_not_lose_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.jsonl")
            context = multiprocessing.get_context("fork")
            start_event = context.Event()
            processes = [
                context.Process(target=_concurrent_upsert, args=(path, start_event, index))
                for index in range(6)
            ]
            for process in processes:
                process.start()
            start_event.set()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)

            rows = load_history_rows(path)
            self.assertEqual(len(rows), 6)
            self.assertEqual(
                [row["repo"] for row in rows],
                [f"org/repo-{index}" for index in range(6)],
            )


class HistoryLoadingTests(unittest.TestCase):
    def test_loads_one_file_or_directory_of_monthly_jsonl_and_skips_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            july = root / "2026-07.jsonl"
            august = root / "nested" / "2026-08.jsonl"
            august.parent.mkdir()
            july.write_text(
                '\n{"date":"2026-07-13","repo":"org/zeta","stars":2}\n\n',
                encoding="utf-8",
            )
            august.write_text(
                '{"date":"2026-08-01","repo":"org/alpha","stars":3,"label":"中文"}\n',
                encoding="utf-8",
            )

            self.assertEqual(len(load_history_rows(july)), 1)
            loaded = load_history_rows(root)
            self.assertEqual(
                [(row["date"], row["repo"]) for row in loaded],
                [("2026-07-13", "org/zeta"), ("2026-08-01", "org/alpha")],
            )
            self.assertEqual(loaded[1]["label"], "中文")

    def test_reports_safe_path_and_line_for_corrupt_or_missing_fields(self):
        bad_rows = (
            ('{"secret":"DO_NOT_ECHO"', "DO_NOT_ECHO"),
            ('{"date":"2026-07-13","repo":"org/tool","secret":"HIDDEN"}', "HIDDEN"),
        )
        for line, secret in bad_rows:
            with self.subTest(line=line):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "bad.jsonl"
                    path.write_text("\n" + line + "\n", encoding="utf-8")

                    with self.assertRaises(HistoryError) as caught:
                        load_history_rows(path)

                    message = str(caught.exception)
                    self.assertIn(str(path), message)
                    self.assertIn(":2", message)
                    self.assertNotIn(secret, message)

    def test_directory_rows_follow_path_then_physical_line_order_for_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            earlier = root / "2026-07.jsonl"
            later = root / "2026-08.jsonl"
            earlier.write_text(
                json.dumps(
                    {
                        "date": "2026-07-13",
                        "repo": "org/tool",
                        "stars": 100,
                        "at": "2026-07-13T10:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            later.write_text(
                json.dumps(
                    {
                        "date": "2026-07-13",
                        "repo": "org/tool",
                        "stars": 200,
                        "at": "2026-07-13T08:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            loaded = load_history_rows(root)

            self.assertEqual([row["stars"] for row in loaded], [100, 200])

    def test_explicit_file_list_preserves_caller_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "z.jsonl"
            second = root / "a.jsonl"
            for path, stars in ((first, 100), (second, 200)):
                path.write_text(
                    json.dumps(
                        {
                            "date": "2026-07-13",
                            "repo": "org/tool",
                            "stars": stars,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

            loaded = load_history_rows([first, second])

            self.assertEqual([row["stars"] for row in loaded], [100, 200])

    def test_invalid_utf8_is_reported_as_safe_history_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid-utf8.jsonl"
            path.write_bytes(b"\xff\n")

            with self.assertRaises(HistoryError) as caught:
                load_history_rows(path)

            message = str(caught.exception)
            self.assertIn(str(path), message)
            self.assertIn(":1", message)


if __name__ == "__main__":
    unittest.main()
