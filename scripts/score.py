"""Reusability scoring for normalized repository records."""

import math
import unicodedata
from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any, Dict

from models import RepositoryRecord


WEIGHTS = {
    "license": 20,
    "maintenance": 20,
    "docs": 15,
    "releases": 15,
    "integration": 15,
    "community": 10,
    "ci": 5,
}

SIGNAL_NAMES = frozenset(name for name in WEIGHTS if name != "license")
UNCLEAR_LICENSES = frozenset(
    {
        "",
        "none",
        "null",
        "noassertion",
        "nolicense",
        "other",
        "unknown",
        "unlicensed",
        "n/a",
    }
)


def _normalized_signal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("reusability signal must be a finite number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("reusability signal must be a finite number")
        normalized = Decimal(str(value))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("reusability signal must be a finite number")
        normalized = value
    else:
        normalized = Decimal(value)

    if not normalized.is_finite():
        raise ValueError("reusability signal must be a finite number")
    if normalized <= 0:
        return Decimal("0")
    if normalized >= 1:
        return Decimal("1")
    return normalized


def _normalized_license(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(
        character
        for character in normalized
        if not character.isspace() and unicodedata.category(character) != "Cf"
    ).casefold()


def _has_clear_license(repo: RepositoryRecord) -> bool:
    if not isinstance(repo.license, str):
        raise ValueError("repository license must be text")
    return _normalized_license(repo.license) not in UNCLEAR_LICENSES


def score_reusability(
    repo: RepositoryRecord, signals: Mapping[str, Any]
) -> Dict[str, Any]:
    """Return a weighted score, component points, and hard-risk markers.

    The license component comes only from ``repo.license``. All other signals
    are ratios in the inclusive range 0..1; missing ratios score zero and
    supplied out-of-range ratios are clipped. Invalid values are rejected
    before calculation so NaN and booleans cannot corrupt a report.
    """
    if not isinstance(repo, RepositoryRecord):
        raise ValueError("repo must be a RepositoryRecord")
    if not isinstance(signals, Mapping):
        raise ValueError("signals must be a mapping")

    unknown = set(signals) - SIGNAL_NAMES
    if unknown:
        raise ValueError("signals contain an unsupported field")
    if not isinstance(repo.archived, bool):
        raise ValueError("repository archived flag must be boolean")

    license_clear = _has_clear_license(repo)
    ratios = {"license": Decimal("1") if license_clear else Decimal("0")}
    for name in SIGNAL_NAMES:
        ratios[name] = _normalized_signal(signals.get(name, 0))

    required_precision = max(
        28,
        max(len(value.as_tuple().digits) for value in ratios.values()) + 10,
    )
    with localcontext() as context:
        context.prec = required_precision
        decimal_components = {
            name: ratios[name] * Decimal(weight)
            for name, weight in WEIGHTS.items()
        }
        total = sum(decimal_components.values(), Decimal("0"))
        score = int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    components = {
        name: float(points) for name, points in decimal_components.items()
    }
    components_exact = {
        name: str(points) for name, points in decimal_components.items()
    }

    risks = []
    if not license_clear:
        risks.append("许可证不明确")
    if repo.archived:
        risks.append("仓库已归档")

    return {
        "score": score,
        "components": components,
        "components_exact": components_exact,
        "risks": risks,
    }
