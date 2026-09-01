"""History snapshots, local growth metrics, and cooling signals."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from models import RepositoryRecord
from storage import StorageError, exclusive_file_lock


class HistoryError(ValueError):
    """Raised when history input or persistence is invalid or unsafe to use."""


def parse_time(value: Any) -> datetime:
    """Parse an ISO timestamp and normalize it to UTC.

    Naive timestamps are rejected so window calculations never depend on the
    machine's local timezone. Error messages intentionally omit input values.
    """

    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
        else:
            raise TypeError
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryError("timestamp must be a timezone-aware ISO value") from exc


def _require_stars(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoryError("stars must be a non-negative integer")
    return value


def calculate_local_growth(rows: Iterable[Mapping[str, Any]], days: int) -> Optional[int]:
    """Calculate star growth against the nearest valid local baseline."""

    windows = {1: (18.0, 30.0, 24.0), 7: (144.0, 192.0, 168.0)}
    if isinstance(days, bool) or days not in windows:
        raise HistoryError("days must be 1 or 7")

    parsed_rows: List[Tuple[datetime, int]] = []
    try:
        source_rows = list(rows)
    except TypeError as exc:
        raise HistoryError("history rows must be iterable") from exc

    for row in source_rows:
        if not isinstance(row, Mapping) or "at" not in row or "stars" not in row:
            raise HistoryError("history row requires at and stars")
        parsed_rows.append((parse_time(row["at"]), _require_stars(row["stars"])))

    if len(parsed_rows) < 2:
        return None

    parsed_rows.sort(key=lambda item: item[0])
    latest_at, latest_stars = parsed_rows[-1]
    minimum, maximum, target = windows[days]
    candidates: List[Tuple[float, datetime, int]] = []
    for baseline_at, baseline_stars in parsed_rows[:-1]:
        age_hours = (latest_at - baseline_at).total_seconds() / 3600.0
        if minimum <= age_hours <= maximum:
            candidates.append((abs(age_hours - target), baseline_at, baseline_stars))

    if not candidates:
        return None

    # If two points are equally close, prefer the more recent baseline.
    _, _, baseline_stars = min(
        candidates,
        key=lambda item: (item[0], -item[1].timestamp()),
    )
    return latest_stars - baseline_stars


def calculate_growth_acceleration(
    day_growth: Optional[float], week_growth: Optional[float]
) -> Optional[float]:
    """Compare one-day growth with the seven-day daily average."""

    if day_growth is None or week_growth is None:
        return None
    if isinstance(day_growth, bool) or isinstance(week_growth, bool):
        return None
    if not isinstance(day_growth, (int, float)) or not isinstance(
        week_growth, (int, float)
    ):
        return None
    if week_growth <= 0:
        return None
    return day_growth / (week_growth / 7.0)


def _normalized_mapping(values: Optional[Mapping[Any, Any]]) -> Dict[str, Any]:
    if not values:
        return {}
    if not isinstance(values, Mapping):
        raise HistoryError("cooling inputs must be mappings")
    return {str(key).strip().lower(): value for key, value in values.items()}


def _watchlist_names(watchlist: Optional[Iterable[Any]]) -> List[str]:
    if watchlist is None:
        return []
    values = watchlist.keys() if isinstance(watchlist, Mapping) else watchlist
    try:
        return sorted(
            {
                str(value).strip().lower()
                for value in values
                if str(value).strip()
            }
        )
    except TypeError as exc:
        raise HistoryError("watchlist must be iterable") from exc


def _rank(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _growth_pair(value: Any) -> Optional[Tuple[float, float]]:
    previous: Any
    current: Any
    if isinstance(value, Mapping):
        previous = value.get("previous_growth", value.get("previous"))
        current = value.get("current_growth", value.get("current"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 2:
            return None
        previous, current = value
    else:
        return None
    if isinstance(previous, bool) or isinstance(current, bool):
        return None
    if not isinstance(previous, (int, float)) or not isinstance(current, (int, float)):
        return None
    return float(previous), float(current)


def _category_drop(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        previous = value.get("previous_rank", value.get("previous"))
        current = value.get("current_rank", value.get("current"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 2:
            return None
        previous, current = value
    else:
        return None
    if isinstance(previous, bool) or isinstance(current, bool):
        return None
    if not isinstance(previous, (int, float)) or not isinstance(current, (int, float)):
        return None
    return float(current - previous)


def _has_consecutive_slowdown(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value >= 2
    if isinstance(value, Mapping):
        for key in ("count", "periods", "consecutive_periods"):
            if key in value:
                return _has_consecutive_slowdown(value[key])
        for key in ("values", "growths", "history"):
            if key in value:
                return _has_consecutive_slowdown(value[key])
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) >= 2 and all(isinstance(item, bool) for item in value[-2:]):
            return bool(value[-2] and value[-1])
        if len(value) >= 3:
            last_three = value[-3:]
            if all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in last_three
            ):
                return last_three[0] > last_three[1] > last_three[2]
    return False


def detect_cooling(
    previous_ranks: Mapping[str, Any],
    current_ranks: Mapping[str, Any],
    growth_pairs: Mapping[str, Any],
    category_rank_drops: Optional[Mapping[str, Any]] = None,
    consecutive_slowdown: Optional[Mapping[str, Any]] = None,
    watchlist: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Return deterministic, structured cooling signals without causal prose."""

    previous = _normalized_mapping(previous_ranks)
    current = _normalized_mapping(current_ranks)
    growth = _normalized_mapping(growth_pairs)
    category = _normalized_mapping(category_rank_drops)
    slowdown = _normalized_mapping(consecutive_slowdown)
    watched = set(_watchlist_names(watchlist))
    repos = sorted(set(previous) | watched)

    signals: List[Dict[str, Any]] = []
    for repo in repos:
        previous_rank = _rank(previous.get(repo))
        current_rank = _rank(current.get(repo))
        pair = _growth_pair(growth.get(repo))
        rank_drop = (
            previous_rank is not None
            and previous_rank <= 20
            and (repo not in current or (current_rank is not None and current_rank > 50))
        )
        growth_drop_ratio = None
        if pair is not None and pair[0] > 0:
            drop_ratio = (pair[0] - pair[1]) / pair[0]
            if drop_ratio >= 0.7:
                growth_drop_ratio = drop_ratio

        if rank_drop and growth_drop_ratio is not None:
            signals.append(
                {
                    "repo": repo,
                    "reason_code": "rank_and_growth_drop",
                    "previous_rank": previous_rank,
                    "current_rank": current_rank,
                    "growth_drop_ratio": growth_drop_ratio,
                }
            )
        elif rank_drop:
            signals.append(
                {
                    "repo": repo,
                    "reason_code": "fell_out_of_top50",
                    "previous_rank": previous_rank,
                    "current_rank": current_rank,
                }
            )
        elif growth_drop_ratio is not None:
            signals.append(
                {
                    "repo": repo,
                    "reason_code": "growth_drop_70pct",
                    "growth_drop_ratio": growth_drop_ratio,
                }
            )

        drop = _category_drop(category.get(repo))
        if drop is not None and drop > 10:
            signals.append(
                {
                    "repo": repo,
                    "reason_code": "category_rank_drop",
                    "category_rank_drop": drop,
                }
            )

        if _has_consecutive_slowdown(slowdown.get(repo)):
            signals.append(
                {"repo": repo, "reason_code": "consecutive_slowdown"}
            )

        if repo in watched and repo not in current:
            signals.append({"repo": repo, "reason_code": "watchlist_missing"})

    return signals


def _validate_date(value: Any) -> str:
    if not isinstance(value, str):
        raise HistoryError("date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HistoryError("date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise HistoryError("date must use YYYY-MM-DD")
    return value


def _validate_history_row(
    row: Any, *, source: Optional[Path] = None, line_number: Optional[int] = None
) -> Dict[str, Any]:
    location = f"{source}:{line_number}: " if source is not None else ""
    try:
        if not isinstance(row, Mapping):
            raise HistoryError("history row must be an object")
        if not {"date", "repo", "stars"}.issubset(row):
            raise HistoryError("history row requires date, repo, and stars")
        validated = copy.deepcopy(dict(row))
        validated["date"] = _validate_date(validated["date"])
        if not isinstance(validated["repo"], str):
            raise HistoryError("repo must use owner/repo format")
        try:
            validated["repo"] = RepositoryRecord(
                full_name=validated["repo"]
            ).full_name
        except (TypeError, ValueError) as exc:
            raise HistoryError("repo must use owner/repo format") from exc
        validated["stars"] = _require_stars(validated["stars"])
        return validated
    except HistoryError as exc:
        if source is None:
            raise
        raise HistoryError(location + str(exc)) from exc


def _resolve_jsonl_paths(paths_or_dir: Union[str, os.PathLike, Iterable[Any]]) -> List[Path]:
    if isinstance(paths_or_dir, (str, os.PathLike)):
        supplied = [Path(paths_or_dir)]
    else:
        try:
            supplied = [Path(item) for item in paths_or_dir]
        except (TypeError, ValueError) as exc:
            raise HistoryError("history paths are invalid") from exc

    resolved: List[Path] = []
    seen = set()
    for path in supplied:
        if path.is_dir():
            candidates = sorted(
                (candidate for candidate in path.rglob("*.jsonl") if candidate.is_file()),
                key=lambda candidate: str(candidate),
            )
        elif path.is_file():
            candidates = [path]
        else:
            raise HistoryError(f"{path}: history path does not exist")
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                resolved.append(candidate)
    return resolved


def load_history_rows(
    paths_or_dir: Union[str, os.PathLike, Iterable[Any]]
) -> List[Dict[str, Any]]:
    """Load validated rows from one file, many files, or a directory tree."""

    rows: List[Dict[str, Any]] = []
    for path in _resolve_jsonl_paths(paths_or_dir):
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError as exc:
            raise HistoryError(f"{path}: unable to read history") from exc
        with handle:
            line_number = 0
            while True:
                line_number += 1
                try:
                    line = handle.readline()
                except UnicodeError as exc:
                    raise HistoryError(f"{path}:{line_number}: invalid UTF-8") from exc
                if line == "":
                    break
                if not line.strip():
                    continue
                try:
                    decoded = json.loads(line)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise HistoryError(f"{path}:{line_number}: invalid JSON") from exc
                validated = _validate_history_row(
                    decoded, source=path, line_number=line_number
                )
                rows.append(validated)

    return rows


@contextmanager
def _exclusive_lock(target: Path):
    try:
        with exclusive_file_lock(target):
            yield
    except StorageError as exc:
        raise HistoryError("history lock operation failed") from exc


def _write_jsonl_atomic(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            for row in rows:
                handle.write(
                    json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                )
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # The file replacement is already durable on common local filesystems;
            # directory fsync is a best-effort metadata durability enhancement.
            pass
    except (OSError, TypeError, ValueError) as exc:
        raise HistoryError(f"{path}: unable to write history atomically") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def upsert_history_rows(
    path: Union[str, os.PathLike], run_date: str, rows: Iterable[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """Upsert one run under a process-safe lock and atomically replace the file."""

    target = Path(path)
    validated_date = _validate_date(run_date)
    try:
        incoming_source = list(rows)
    except TypeError as exc:
        raise HistoryError("history rows must be iterable") from exc

    incoming: List[Dict[str, Any]] = []
    for row in incoming_source:
        if not isinstance(row, Mapping):
            raise HistoryError("history row must be an object")
        candidate = copy.deepcopy(dict(row))
        candidate["date"] = validated_date
        incoming.append(_validate_history_row(candidate))

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HistoryError(f"{target.parent}: unable to create history directory") from exc

    with _exclusive_lock(target):
        existing = load_history_rows(target) if target.exists() else []
        merged: Dict[Tuple[str, str], Dict[str, Any]] = {
            (row["date"], row["repo"]): row for row in existing
        }
        for row in incoming:
            merged[(row["date"], row["repo"])] = row
        result = [merged[key] for key in sorted(merged)]
        _write_jsonl_atomic(target, result)
        return copy.deepcopy(result)
