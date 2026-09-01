"""Fetch and normalize repository trend counts from OSSInsight."""

from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlencode

from http_client import SourceError, get_json
from models import RepositoryRecord


OSSINSIGHT_TRENDS_API = "https://api.ossinsight.io/v1/trends/repos/"
PERIODS = {
    "24h": ("past_24_hours", "stars_24h_external"),
    "7d": ("past_week", "stars_7d_external"),
    "30d": ("past_month", "stars_30d_external"),
}


def _integer(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_trend_rows(payload: Any, period: str) -> List[RepositoryRecord]:
    """Convert one OSSInsight period without filling unrelated metrics."""

    if period not in PERIODS:
        raise ValueError("period must be one of: 24h, 7d, 30d")
    metric_field = PERIODS[period][1]
    if not isinstance(payload, Mapping):
        raise SourceError("OSSInsight response had invalid structure")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise SourceError("OSSInsight response had invalid structure")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise SourceError("OSSInsight response had invalid structure")

    records: List[RepositoryRecord] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        full_name = (
            row.get("repo_name")
            or row.get("full_name")
            or row.get("repository_name")
            or ""
        )
        stars = _integer(row.get("stars"))
        if stars is None:
            continue
        metrics: Dict[str, int] = {metric_field: stars}
        try:
            record = RepositoryRecord(
                full_name=str(full_name),
                description=str(row.get("description") or ""),
                primary_language=str(row.get("primary_language") or ""),
                source_records=[
                    {
                        "source": "ossinsight",
                        "scope": "selected_set",
                        "period": period,
                    }
                ],
                **metrics,
            )
        except ValueError:
            continue
        records.append(record)
    return records


def fetch_trend(period: str = "24h") -> List[RepositoryRecord]:
    """Fetch one supported OSSInsight repository trend period."""

    if period not in PERIODS:
        raise ValueError("period must be one of: 24h, 7d, 30d")
    parameters = urlencode({"period": PERIODS[period][0], "language": "All"})
    payload = get_json(f"{OSSINSIGHT_TRENDS_API}?{parameters}")
    return parse_trend_rows(payload, period)
