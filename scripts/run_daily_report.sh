#!/usr/bin/env bash
set -euo pipefail

# Daily GitHub trend radar report runner (Asia/Shanghai local time via launchd).
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
WORKSPACE="${RADAR_WORKSPACE:-$PROJECT_ROOT}"
RADAR_OUTPUT_ROOT="${RADAR_OUTPUT_ROOT:-$WORKSPACE/GitHub开源趋势雷达}"
RADAR_PY="${RADAR_PY:-$PROJECT_ROOT/scripts/radar.py}"
PYTHON="${PYTHON_BIN:-$(command -v python3)}"
CERTIFI_PEM="${CERTIFI_PEM:-}"
LOG_DIR="$RADAR_OUTPUT_ROOT/运行状态/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/daily-report-$STAMP.log"
LATEST_LOG="$LOG_DIR/daily-report-latest.log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE" | tee "$LATEST_LOG") 2>&1

echo "==== github-trend-radar daily report start $(date '+%Y-%m-%d %H:%M:%S %z') ===="
echo "workspace=$WORKSPACE"
echo "radar_output_root=$RADAR_OUTPUT_ROOT"
echo "python=$PYTHON"
echo "ssl_cert_file=${CERTIFI_PEM:-<system-default>}"

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: python not found or not executable: $PYTHON" >&2
  exit 1
fi
if [[ ! -f "$RADAR_PY" ]]; then
  echo "ERROR: radar.py not found: $RADAR_PY" >&2
  exit 1
fi
if [[ -n "$CERTIFI_PEM" ]]; then
  if [[ ! -f "$CERTIFI_PEM" ]]; then
    echo "ERROR: certifi CA bundle not found: $CERTIFI_PEM" >&2
    exit 1
  fi
  export SSL_CERT_FILE="$CERTIFI_PEM"
  export REQUESTS_CA_BUNDLE="$CERTIFI_PEM"
fi

# Optional: pick up GITHUB_TOKEN from environment if present for higher rate limits.
# Without token, unauthenticated GitHub API still works with lower limits.

cd "$WORKSPACE"
"$PYTHON" "$RADAR_PY" --output-root "$RADAR_OUTPUT_ROOT" report
exit_status=$?

RUN_DATE="$(TZ=Asia/Shanghai date +%F)"
REPORT_MD="$RADAR_OUTPUT_ROOT/最新报告/GitHub开源趋势与项目复用雷达-$RUN_DATE.md"
REPORT_JSON="$RADAR_OUTPUT_ROOT/最新报告/原始数据-$RUN_DATE.json"
REPORT_HTML="$RADAR_OUTPUT_ROOT/最新报告/GitHub开源趋势与项目复用雷达-$RUN_DATE.html"

echo "exit_code=$exit_status"
if [[ -f "$REPORT_MD" ]]; then
  echo "report_md=$REPORT_MD"
  echo "report_md_bytes=$(wc -c < "$REPORT_MD" | tr -d ' ')"
  if stat -f '%Sm' "$REPORT_MD" >/dev/null 2>&1; then
    echo "report_md_mtime=$(stat -f '%Sm' "$REPORT_MD")"
  else
    echo "report_md_mtime=$(stat -c '%y' "$REPORT_MD")"
  fi
fi
if [[ -f "$REPORT_JSON" ]]; then
  echo "report_json=$REPORT_JSON"
  echo "report_json_bytes=$(wc -c < "$REPORT_JSON" | tr -d ' ')"
fi
if [[ -f "$REPORT_HTML" ]]; then
  echo "report_html=$REPORT_HTML"
  echo "report_html_bytes=$(wc -c < "$REPORT_HTML" | tr -d ' ')"
fi
echo "==== github-trend-radar daily report end $(date '+%Y-%m-%d %H:%M:%S %z') ===="
exit $exit_status
