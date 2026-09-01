# GitHub Trend Radar

GitHub Trend Radar collects GitHub Trending, GitHub Search, and OSS Insight data to produce reproducible reports for:

- total stars, 24-hour growth, and 7-day growth;
- category trends and project descriptions;
- reuse potential scoring;
- watchlist maintenance and historical changes;
- evidence-based replacement analysis.

The project is both a standalone Python CLI and a Codex skill. It does not require authentication for basic GitHub API access. Set `GITHUB_TOKEN` when a higher API rate limit is needed. Tokens and request headers are never written to reports.

## Requirements

- Python 3.10+
- Dependencies in `requirements.txt`

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

## Usage

Run commands from the repository root:

```bash
python3 scripts/radar.py --help
python3 scripts/radar.py report
python3 scripts/radar.py repo openai/codex
python3 scripts/radar.py compare old-owner/old-repo new-owner/new-repo
python3 scripts/radar.py watchlist
```

Use `--workspace` for a workspace-specific data directory or `--output-root` for a direct report directory. Generated reports, history, Excel files, and runtime state should remain outside version control.

The bundled `scripts/run_daily_report.sh` is portable. Configure `RADAR_WORKSPACE`, `RADAR_OUTPUT_ROOT`, `RADAR_PY`, `PYTHON_BIN`, and optionally `CERTIFI_PEM` through the environment instead of editing the script.

## Data sources and limitations

The radar keeps data sources and periods separate. GitHub Search `created_at` is not used as a substitute for star growth. Source outages are reported as degradations; the tool does not silently fabricate missing metrics. GitHub Trending and OSS Insight are external services and may change their response formats or rate limits.

## Testing

The test suite uses the Python standard-library `unittest` runner:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## Codex skill

`SKILL.md` contains the Codex workflow and triggering metadata. `agents/openai.yaml` provides UI metadata, while `references/` contains metric and report definitions.

## License

MIT. See [LICENSE](LICENSE).
