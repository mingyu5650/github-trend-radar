#!/usr/bin/env python3
"""Offline tests for radar HTTP and source fetchers."""

import http.client
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit


TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fetch_github import (
    fetch_readme_sections, fetch_repo_details, fetch_repo_star_count,
    fetch_top_repositories,
    parse_readme_sections, parse_search_rows,
)
from fetch_ossinsight import fetch_trend, parse_trend_rows
from fetch_trending import fetch_trending, parse_trending_html
from http_client import SourceError, get_json, get_text, github_token


def load_json(name):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def source_record(record, source):
    return next(item for item in record.source_records if item["source"] == source)


class FixtureContractTests(unittest.TestCase):
    @patch("fetch_github.get_text")
    def test_public_repository_page_star_fallback(self, get_text_mock):
        get_text_mock.return_value = (
            '<a aria-label="10,141 users starred this repository"></a>'
        )

        record = fetch_repo_star_count("block/buzz")

        self.assertEqual(record.total_stars, 10141)
        self.assertEqual(
            source_record(record, "github_web")["scope"],
            "repository_star_fallback",
        )

    def test_readme_sections_extract_features_and_use_cases(self):
        sections = parse_readme_sections("""
## ✨ Features
- **Easy integration**
  - No headless browser
- Text-based DOM manipulation
## 💡 Use Cases
- **Smart Form Filling** — ERP and CRM workflows
- Multi-page Agent
## Other
- ignored
""")
        self.assertEqual(
            sections["features"],
            ["Easy integration", "No headless browser", "Text-based DOM manipulation"],
        )
        self.assertEqual(
            sections["use_cases"],
            ["Smart Form Filling — ERP and CRM workflows", "Multi-page Agent"],
        )

    @patch("fetch_github.get_text")
    def test_readme_fetch_prefers_chinese_variant(self, get_text_mock):
        get_text_mock.return_value = "## 特性\n- 纯页面内 JavaScript\n## 应用场景\n- 智能表单填写"

        sections = fetch_readme_sections("org/tool")

        self.assertEqual(sections["features"], ["纯页面内 JavaScript"])
        self.assertEqual(sections["use_cases"], ["智能表单填写"])
        self.assertEqual(sections["source"], ["README:main/docs/README-zh.md"])

    def test_github_search_fixture_matches_exact_contract(self):
        self.assertEqual(
            load_json("github_search.json"),
            {
                "items": [
                    {
                        "full_name": "org/tool",
                        "html_url": "https://github.com/org/tool",
                        "description": "AI coding CLI",
                        "stargazers_count": 2000,
                        "forks_count": 100,
                        "language": "Python",
                        "topics": ["ai", "cli"],
                        "license": {"spdx_id": "MIT"},
                        "created_at": "2026-01-01T00:00:00Z",
                        "pushed_at": "2026-07-13T00:00:00Z",
                        "archived": False,
                        "open_issues_count": 12,
                    },
                    {
                        "full_name": "org/awesome-ai",
                        "html_url": "https://github.com/org/awesome-ai",
                        "description": "A curated awesome list",
                        "stargazers_count": 1500,
                        "forks_count": 80,
                        "language": None,
                        "topics": ["awesome-list"],
                        "license": {"spdx_id": "CC0-1.0"},
                        "created_at": "2025-01-01T00:00:00Z",
                        "pushed_at": "2026-07-10T00:00:00Z",
                        "archived": False,
                        "open_issues_count": 2,
                    },
                ]
            },
        )

    def test_github_repo_fixture_contains_required_contract(self):
        fixture = load_json("github_repo.json")
        required = {
            "full_name": "org/tool",
            "html_url": "https://github.com/org/tool",
            "description": "AI coding CLI",
            "stargazers_count": 2000,
            "forks_count": 100,
            "open_issues_count": 12,
            "default_branch": "main",
            "license": {"spdx_id": "MIT"},
            "topics": ["ai", "cli"],
            "created_at": "2026-01-01T00:00:00Z",
            "pushed_at": "2026-07-13T00:00:00Z",
            "archived": False,
            "language": "Python",
        }

        self.assertEqual({key: fixture.get(key) for key in required}, required)

    def test_ossinsight_fixtures_include_repository_metadata(self):
        for fixture_name in (
            "ossinsight_24h.json",
            "ossinsight_7d.json",
            "ossinsight_30d.json",
        ):
            with self.subTest(fixture=fixture_name):
                row = load_json(fixture_name)["data"]["rows"][0]
                self.assertEqual(row["primary_language"], "Python")
                self.assertEqual(row["description"], "AI coding CLI")


class GitHubParserTests(unittest.TestCase):
    def test_search_maps_fields_and_keeps_stars_as_total_only(self):
        records = parse_search_rows(load_json("github_search.json"))

        self.assertEqual([record.full_name for record in records], ["org/tool", "org/awesome-ai"])
        self.assertGreater(records[0].total_stars, records[1].total_stars)
        first = records[0]
        self.assertEqual(first.repo_url, "https://github.com/org/tool")
        self.assertEqual(first.description, "AI coding CLI")
        self.assertEqual(first.total_stars, 2000)
        self.assertIsNone(first.stars_24h_external)
        self.assertIsNone(first.stars_7d_external)
        self.assertIsNone(first.stars_30d_external)
        self.assertEqual(first.forks, 100)
        self.assertEqual(first.primary_language, "Python")
        self.assertEqual(first.topics, ["ai", "cli"])
        self.assertEqual(first.license, "MIT")
        self.assertEqual(first.created_at, "2026-01-01T00:00:00Z")
        self.assertEqual(first.pushed_at, "2026-07-13T00:00:00Z")
        self.assertFalse(first.archived)
        self.assertEqual(first.open_issues, 12)
        self.assertEqual(source_record(first, "github")["scope"], "search")

    @patch("fetch_github.github_token", return_value="secret-token")
    @patch("fetch_github.get_json")
    def test_top_repositories_uses_search_parameters_and_auth_header(self, get_json_mock, _token_mock):
        get_json_mock.return_value = load_json("github_search.json")

        records = fetch_top_repositories(top=2, query="stars:>100")

        self.assertEqual(len(records), 2)
        url = get_json_mock.call_args.args[0]
        headers = get_json_mock.call_args.kwargs["headers"]
        self.assertIn("q=stars%3A%3E100", url)
        self.assertIn("sort=stars", url)
        self.assertIn("order=desc", url)
        self.assertIn("per_page=2", url)
        self.assertEqual(headers["Authorization"], "Bearer secret-token")

    @patch("fetch_github.github_token", return_value="")
    @patch("fetch_github.get_json")
    def test_repo_detail_survives_missing_latest_release(self, get_json_mock, _token_mock):
        get_json_mock.side_effect = [
            load_json("github_repo.json"),
            SourceError("HTTP request failed", status_code=404),
        ]

        record = fetch_repo_details("org/tool")

        self.assertEqual(record.full_name, "org/tool")
        self.assertEqual(record.total_stars, 2000)
        self.assertEqual(record.description, "AI coding CLI")
        self.assertEqual(record.latest_release, "")
        self.assertEqual(source_record(record, "github")["release"], "none_or_unavailable")
        release_record = source_record(record, "github_latest_release")
        self.assertEqual(release_record["status"], "none_or_unavailable")
        self.assertEqual(release_record["status_code"], 404)

    @patch("fetch_github.github_token", return_value="")
    @patch("fetch_github.get_json")
    def test_repo_detail_records_successful_latest_release(self, get_json_mock, _token_mock):
        get_json_mock.side_effect = [
            load_json("github_repo.json"),
            {
                "tag_name": "v1.2.3",
                "published_at": "2026-07-13T01:02:03Z",
            },
        ]

        record = fetch_repo_details("org/tool")

        self.assertEqual(record.latest_release, "v1.2.3")
        self.assertEqual(record.latest_release_at, "2026-07-13T01:02:03Z")
        release_record = source_record(record, "github_latest_release")
        self.assertEqual(release_record["status"], "available")
        self.assertNotIn("status_code", release_record)

    @patch("fetch_github.github_token", return_value="")
    @patch("fetch_github.get_json")
    def test_repo_detail_distinguishes_release_fetch_failures(self, get_json_mock, _token_mock):
        for status_code in (403, 500):
            with self.subTest(status_code=status_code):
                get_json_mock.side_effect = [
                    load_json("github_repo.json"),
                    SourceError(
                        "secret https://api.github.com/?token=do-not-leak",
                        status_code=status_code,
                    ),
                ]

                record = fetch_repo_details("org/tool")

                self.assertEqual(record.latest_release, "")
                self.assertEqual(source_record(record, "github")["release"], "release_fetch_failed")
                release_record = source_record(record, "github_latest_release")
                self.assertEqual(
                    release_record,
                    {
                        "source": "github_latest_release",
                        "status": "release_fetch_failed",
                        "status_code": status_code,
                    },
                )
                serialized = json.dumps(record.to_dict())
                self.assertNotIn("do-not-leak", serialized)
                self.assertNotIn("api.github.com", serialized)

    def test_search_ignores_non_list_topics_and_non_boolean_archived_values(self):
        payload = {
            "items": [
                {"full_name": "org/first", "topics": "ai", "archived": "false"},
                {"full_name": "org/second", "topics": 123, "archived": {"value": True}},
            ]
        }

        records = parse_search_rows(payload)

        self.assertEqual([record.full_name for record in records], ["org/first", "org/second"])
        self.assertEqual([record.topics for record in records], [[], []])
        self.assertEqual([record.archived for record in records], [False, False])


class OSSInsightParserTests(unittest.TestCase):
    def test_each_period_writes_only_its_matching_external_metric(self):
        cases = (
            ("24h", "ossinsight_24h.json", "stars_24h_external", 120),
            ("7d", "ossinsight_7d.json", "stars_7d_external", 640),
            ("30d", "ossinsight_30d.json", "stars_30d_external", 1800),
        )
        metric_fields = {"stars_24h_external", "stars_7d_external", "stars_30d_external"}

        for period, fixture, expected_field, expected_value in cases:
            with self.subTest(period=period):
                record = parse_trend_rows(load_json(fixture), period)[0]
                self.assertEqual(getattr(record, expected_field), expected_value)
                self.assertEqual(record.primary_language, "Python")
                self.assertEqual(record.description, "AI coding CLI")
                for field in metric_fields - {expected_field}:
                    self.assertIsNone(getattr(record, field))

    def test_rejects_invalid_period(self):
        with self.assertRaises(ValueError):
            parse_trend_rows(load_json("ossinsight_24h.json"), "weekly")

    def test_rejects_invalid_payload_structure_with_safe_source_error(self):
        invalid_payloads = (
            None,
            {},
            {"data": None},
            {"data": []},
            {"data": {"rows": None}},
            {"data": {"rows": {"unexpected": "mapping"}}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(SourceError) as raised:
                    parse_trend_rows(payload, "24h")
                self.assertEqual(str(raised.exception), "OSSInsight response had invalid structure")

    @patch("fetch_ossinsight.get_json")
    def test_fetch_trend_builds_each_period_query_without_network(self, get_json_mock):
        cases = (
            ("24h", "past_24_hours", "ossinsight_24h.json"),
            ("7d", "past_week", "ossinsight_7d.json"),
            ("30d", "past_month", "ossinsight_30d.json"),
        )
        for period, query_period, fixture_name in cases:
            with self.subTest(period=period):
                get_json_mock.reset_mock()
                get_json_mock.return_value = load_json(fixture_name)

                records = fetch_trend(period)

                self.assertEqual(len(records), 1)
                url = get_json_mock.call_args.args[0]
                parsed = urlsplit(url)
                self.assertEqual(
                    f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                    "https://api.ossinsight.io/v1/trends/repos/",
                )
                self.assertEqual(
                    parse_qs(parsed.query),
                    {"period": [query_period], "language": ["All"]},
                )


class GitHubTrendingParserTests(unittest.TestCase):
    def test_weekly_maps_repository_total_and_selected_set_provenance(self):
        html = (FIXTURES_DIR / "github_trending.html").read_text(encoding="utf-8")

        record = parse_trending_html(html, "weekly")[0]

        self.assertEqual(record.full_name, "org/tool")
        self.assertEqual(record.description, "AI coding CLI")
        self.assertEqual(record.total_stars, 2000)
        self.assertEqual(record.stars_7d_external, 640)
        provenance = source_record(record, "github_trending")
        self.assertEqual(provenance["scope"], "selected_set")
        self.assertEqual(provenance["period"], "weekly")

    def test_daily_and_monthly_write_only_the_matching_metric(self):
        html = (FIXTURES_DIR / "github_trending.html").read_text(encoding="utf-8")

        daily = parse_trending_html(html.replace("this week", "today"), "daily")[0]
        monthly = parse_trending_html(html.replace("this week", "this month"), "monthly")[0]

        self.assertEqual(daily.stars_24h_external, 640)
        self.assertIsNone(daily.stars_7d_external)
        self.assertIsNone(daily.stars_30d_external)
        self.assertEqual(monthly.stars_30d_external, 640)
        self.assertIsNone(monthly.stars_24h_external)
        self.assertIsNone(monthly.stars_7d_external)

    def test_each_period_accepts_singular_star_wording(self):
        weekly_html = (FIXTURES_DIR / "github_trending.html").read_text(encoding="utf-8")
        cases = (
            ("daily", "1 star today", "stars_24h_external"),
            ("weekly", "1 star this week", "stars_7d_external"),
            ("monthly", "1 star this month", "stars_30d_external"),
        )
        for period, wording, metric_field in cases:
            with self.subTest(period=period):
                html = weekly_html.replace("640 stars this week", wording)

                record = parse_trending_html(html, period)[0]

                self.assertEqual(getattr(record, metric_field), 1)

    def test_period_must_match_exact_trend_count_wording(self):
        weekly_html = (FIXTURES_DIR / "github_trending.html").read_text(encoding="utf-8")
        cases = (
            (weekly_html, "daily"),
            (weekly_html, "monthly"),
            (weekly_html.replace("this week", "today"), "weekly"),
            (weekly_html.replace("this week", "this month"), "weekly"),
            (weekly_html.replace("this week", "this week extra"), "weekly"),
        )
        for html, period in cases:
            with self.subTest(period=period, html=html):
                self.assertEqual(parse_trending_html(html, period), [])

    def test_uses_only_title_repository_link_and_designated_trend_span(self):
        html = (FIXTURES_DIR / "github_trending.html").read_text(encoding="utf-8")
        html = html.replace(
            '<h2 class="h3 lh-condensed">',
            '<a href="/wrong/repository">distractor</a><h2 class="h3 lh-condensed">',
        ).replace(
            ">AI coding CLI</p>",
            ">999 stars this week</p>",
        )

        record = parse_trending_html(html, "weekly")[0]

        self.assertEqual(record.full_name, "org/tool")
        self.assertEqual(record.stars_7d_external, 640)

    def test_rejects_trend_count_span_without_both_required_classes(self):
        html = (FIXTURES_DIR / "github_trending.html").read_text(encoding="utf-8")
        for replacement in ('class="d-inline-block"', 'class="float-sm-right"'):
            with self.subTest(replacement=replacement):
                malformed = html.replace(
                    'class="d-inline-block float-sm-right"',
                    replacement,
                )
                self.assertEqual(parse_trending_html(malformed, "weekly"), [])

    def test_empty_or_malformed_html_returns_no_records(self):
        malformed = (
            "",
            "<html><article class='Box-row'>not a repository</article></html>",
            (FIXTURES_DIR / "github_trending.html")
            .read_text(encoding="utf-8")
            .replace('class="Box-row"', 'class="renamed-row"'),
            (FIXTURES_DIR / "github_trending.html")
            .read_text(encoding="utf-8")
            .replace('href="/org/tool"', 'href="/missing-repository-structure"'),
        )
        for html in malformed:
            with self.subTest(html=html):
                self.assertEqual(parse_trending_html(html, "weekly"), [])

    @patch("fetch_trending.get_text")
    def test_fetch_trending_builds_period_language_and_headers_without_network(self, get_text_mock):
        weekly_html = (FIXTURES_DIR / "github_trending.html").read_text(encoding="utf-8")
        cases = (
            ("daily", "", "https://github.com/trending?since=daily", "today"),
            ("weekly", "Python", "https://github.com/trending/python?since=weekly", "this week"),
            ("monthly", "C++", "https://github.com/trending/c%2B%2B?since=monthly", "this month"),
        )
        for period, language, expected_url, wording in cases:
            with self.subTest(period=period, language=language):
                get_text_mock.reset_mock()
                get_text_mock.return_value = weekly_html.replace("this week", wording)

                records = fetch_trending(period, language)

                self.assertEqual(len(records), 1)
                get_text_mock.assert_called_once_with(
                    expected_url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "User-Agent": "github-trend-radar",
                    },
                )


class HttpClientTests(unittest.TestCase):
    def test_get_text_rejects_c0_header_characters_without_network(self):
        cases = (
            {"Authorization": "Bearer secret-token\rInjected: yes"},
            {"Authorization": "Bearer secret-token\nInjected: yes"},
            {"Authorization": "Bearer secret-token\x00"},
            {"X-Trace": "secret-header-value\x1f"},
            {"X-\nSecret": "secret-header-value"},
        )
        for headers in cases:
            with self.subTest(headers=list(headers)):
                with patch(
                    "http_client.urllib.request.urlopen",
                    side_effect=AssertionError("network must not be called"),
                ) as urlopen_mock:
                    with self.assertRaises(SourceError) as raised:
                        get_text("https://example.invalid/data", headers=headers)

                urlopen_mock.assert_not_called()
                self.assertNotIn("secret", str(raised.exception).lower())
                self.assertNotIn("injected", str(raised.exception).lower())

    @patch("http_client.urllib.request.urlopen")
    def test_authorization_is_initial_only_and_not_copied_by_redirect(self, urlopen_mock):
        response = Mock()
        response.read.return_value = b"ok"
        response.headers.get_content_charset.return_value = "utf-8"
        urlopen_mock.return_value.__enter__.return_value = response

        self.assertEqual(
            get_text(
                "https://api.github.com/repos/org/tool",
                headers={
                    "Authorization": "Bearer secret-token",
                    "Accept": "application/json",
                },
            ),
            "ok",
        )

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertIn("Authorization", request.unredirected_hdrs)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.get_header("Accept"), "application/json")
        redirected = urllib.request.HTTPRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.invalid/redirected",
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))
        self.assertEqual(redirected.get_header("Accept"), "application/json")

    def test_get_text_wraps_request_and_urlopen_value_errors(self):
        with patch(
            "http_client.urllib.request.Request",
            side_effect=ValueError("secret request diagnostic"),
        ), patch(
            "http_client.urllib.request.urlopen",
            side_effect=AssertionError("network must not be called"),
        ) as urlopen_mock:
            with self.assertRaises(SourceError) as raised:
                get_text("https://example.invalid/data")
        urlopen_mock.assert_not_called()
        self.assertEqual(str(raised.exception), "HTTP request failed")
        self.assertNotIn("secret", repr(raised.exception).lower())

        with patch(
            "http_client.urllib.request.urlopen",
            side_effect=ValueError("secret URL or header diagnostic"),
        ):
            with self.assertRaises(SourceError) as raised:
                get_text("https://example.invalid/data")
        self.assertEqual(str(raised.exception), "HTTP request failed")
        self.assertNotIn("secret", repr(raised.exception).lower())

    @patch("http_client.urllib.request.urlopen", side_effect=urllib.error.URLError("offline"))
    def test_get_text_wraps_network_errors(self, _urlopen_mock):
        with self.assertRaises(SourceError) as raised:
            get_text("https://example.invalid/data")

        self.assertNotIn("token", str(raised.exception).lower())

    def test_get_text_exposes_only_safe_http_status(self):
        error = urllib.error.HTTPError(
            "https://example.invalid/data?token=secret-token",
            403,
            "forbidden secret-diagnostic",
            {"Authorization": "Bearer secret-token"},
            io.BytesIO(b"secret response body"),
        )
        with patch("http_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SourceError) as raised:
                get_text("https://example.invalid/data?token=secret-token")

        self.assertEqual(str(raised.exception), "HTTP request failed with status 403")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertNotIn("secret", repr(raised.exception).lower())

    def test_get_text_wraps_context_and_read_failures(self):
        failures = (
            OSError("secret context diagnostic"),
            http.client.IncompleteRead(b"secret partial body", 100),
            http.client.HTTPException("secret protocol diagnostic"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                response = Mock()
                response.read.side_effect = failure
                with patch("http_client.urllib.request.urlopen") as urlopen_mock:
                    urlopen_mock.return_value.__enter__.return_value = response
                    with self.assertRaises(SourceError) as raised:
                        get_text("https://example.invalid/data?token=secret-token")

                self.assertEqual(str(raised.exception), "HTTP request failed")
                self.assertIsNone(raised.exception.status_code)
                self.assertNotIn("secret", repr(raised.exception).lower())

        with patch("http_client.urllib.request.urlopen") as urlopen_mock:
            urlopen_mock.return_value.__enter__.side_effect = OSError(
                "secret context manager diagnostic"
            )
            with self.assertRaises(SourceError) as raised:
                get_text("https://example.invalid/data?token=secret-token")
        self.assertEqual(str(raised.exception), "HTTP request failed")

    @patch("http_client.urllib.request.urlopen")
    def test_get_json_wraps_invalid_json(self, urlopen_mock):
        response = Mock()
        response.read.return_value = b"not-json"
        response.headers.get_content_charset.return_value = "utf-8"
        urlopen_mock.return_value.__enter__.return_value = response

        with self.assertRaises(SourceError):
            get_json("https://example.invalid/data")

    @patch("http_client.urllib.request.urlopen")
    def test_get_json_does_not_expose_invalid_body(self, urlopen_mock):
        response = Mock()
        response.read.return_value = b'{"token": "secret", invalid}'
        response.headers.get_content_charset.return_value = "utf-8"
        urlopen_mock.return_value.__enter__.return_value = response

        with self.assertRaises(SourceError) as raised:
            get_json("https://example.invalid/data")

        self.assertEqual(str(raised.exception), "HTTP response was not valid JSON")
        self.assertNotIn("secret", repr(raised.exception).lower())

    @patch.dict(os.environ, {}, clear=True)
    @patch("http_client.subprocess.run", side_effect=FileNotFoundError)
    def test_github_token_returns_empty_when_gh_is_missing(self, _run_mock):
        self.assertEqual(github_token(), "")

    @patch.dict(os.environ, {"GITHUB_TOKEN": " env-token "}, clear=True)
    @patch("http_client.subprocess.run", side_effect=AssertionError("must not call gh"))
    def test_github_token_prefers_environment_without_calling_gh(self, run_mock):
        self.assertEqual(github_token(), "env-token")
        run_mock.assert_not_called()

    @patch.dict(os.environ, {}, clear=True)
    @patch("http_client.subprocess.run")
    def test_github_token_returns_empty_on_failure_without_leaking_stderr(self, run_mock):
        run_mock.return_value = subprocess.CompletedProcess(
            ["gh", "auth", "token"], 1, stdout="", stderr="secret-diagnostic"
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(github_token(), "")

        self.assertNotIn("secret-diagnostic", stdout.getvalue())
        self.assertNotIn("secret-diagnostic", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
