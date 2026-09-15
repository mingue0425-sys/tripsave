"""Pure feature-normalization helpers for the V1.0 ranker."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RobustBounds:
    """Percentile bounds used to make a candidate-set comparison robust."""

    low: float
    high: float
    median: float


def _finite_values(values: Iterable[float | int | None]) -> list[float]:
    result: list[float] = []
    for value in values:
        if value is None:
            continue
        numeric = float(value)
        if math.isfinite(numeric):
            result.append(numeric)
    return result


def percentile(values: Iterable[float | int | None], fraction: float) -> float | None:
    """Return a deterministic linearly interpolated percentile."""

    if not 0.0 <= fraction <= 1.0:
        raise ValueError("percentile fraction must be between 0 and 1")
    ordered = sorted(_finite_values(values))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    portion = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * portion


def robust_bounds(
    values: Iterable[float | int | None],
    *,
    low_fraction: float = 0.05,
    high_fraction: float = 0.95,
) -> RobustBounds | None:
    """Build P5/P95 bounds; small sets naturally collapse to neutral bounds."""

    materialized = _finite_values(values)
    if not materialized:
        return None
    low = percentile(materialized, low_fraction)
    high = percentile(materialized, high_fraction)
    middle = percentile(materialized, 0.5)
    assert low is not None and high is not None and middle is not None
    return RobustBounds(low=low, high=high, median=middle)


def _normalize(
    value: float | None,
    bounds: RobustBounds | None,
    *,
    lower_is_better: bool,
) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or bounds is None:
        return None
    if bounds.high <= bounds.low:
        return 0.5
    clipped = min(bounds.high, max(bounds.low, numeric))
    ratio = (clipped - bounds.low) / (bounds.high - bounds.low)
    score = 1.0 - ratio if lower_is_better else ratio
    return max(0.0, min(1.0, score))


def normalize_lower_is_better(
    value: float | None,
    values: Iterable[float | int | None] | RobustBounds | None,
    *,
    low_fraction: float = 0.05,
    high_fraction: float = 0.95,
) -> float | None:
    """Map a lower-is-better value to 0..1 using clipped candidate-set stats."""

    bounds = values if isinstance(values, RobustBounds) else robust_bounds(
        values or [], low_fraction=low_fraction, high_fraction=high_fraction
    )
    return _normalize(value, bounds, lower_is_better=True)


def normalize_higher_is_better(
    value: float | None,
    values: Iterable[float | int | None] | RobustBounds | None,
    *,
    low_fraction: float = 0.05,
    high_fraction: float = 0.95,
) -> float | None:
    """Map a higher-is-better value to 0..1 using clipped candidate-set stats."""

    bounds = values if isinstance(values, RobustBounds) else robust_bounds(
        values or [], low_fraction=low_fraction, high_fraction=high_fraction
    )
    return _normalize(value, bounds, lower_is_better=False)


def normalize_unit(value: float | None) -> float | None:
    """Validate an already normalized value without turning null into zero."""

    if value is None:
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return max(0.0, min(1.0, numeric))


def saturating_count_score(
    count: int | None,
    *,
    scale: float,
) -> float | None:
    """Turn availability counts into a diminishing-return score."""

    if count is None:
        return None
    if count < 0 or scale <= 0 or not math.isfinite(float(scale)):
        raise ValueError("count and scale must be non-negative and finite")
    return max(0.0, min(1.0, 1.0 - math.exp(-float(count) / scale)))


def renormalize_weights(
    weights: Mapping[str, float],
    available_features: Iterable[str],
) -> dict[str, float]:
    """Drop unavailable features and renormalize only the remaining weights."""

    available = set(available_features)
    selected = {
        name: float(value)
        for name, value in weights.items()
        if name in available and value > 0.0 and math.isfinite(float(value))
    }
    total = sum(selected.values())
    if total <= 0.0:
        return {}
    return {name: value / total for name, value in selected.items()}


def weighted_sum(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """Compute a bounded weighted sum from already normalized values."""

    result = sum(values.get(name, 0.0) * weight for name, weight in weights.items())
    return max(0.0, min(1.0, result))


__all__ = [
    "RobustBounds",
    "normalize_higher_is_better",
    "normalize_lower_is_better",
    "normalize_unit",
    "percentile",
    "renormalize_weights",
    "robust_bounds",
    "saturating_count_score",
    "weighted_sum",
]
