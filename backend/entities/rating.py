"""Rating-scale normalization and review-count-aware aggregation."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median

from backend.entities.matcher import coerce_source_record
from backend.entities.models import EntitySourceRecord
from backend.entities.normalize import normalize_text

MINIMUM_CONFIDENCE_REVIEW_COUNT_PERCENTILE = 0.75
RATING_FRESHNESS_HALF_LIFE_DAYS = 180.0


@dataclass(frozen=True)
class RatingObservation:
    source_record_id: str
    normalized_rating: float
    review_count: int | None


@dataclass(frozen=True)
class RatingProfile:
    normalized_rating: float | None
    rating_confidence: float | None
    observed_review_count_sum: int | None
    rating_disagreement: bool
    prior: float | None
    minimum_review_count: int
    observations: tuple[RatingObservation, ...]


def normalize_rating(rating: float | None, rating_scale: float | None) -> float | None:
    """Convert any positive source scale into the internal 0..1 scale."""

    if rating is None:
        return None
    if rating_scale is None or not math.isfinite(rating) or not math.isfinite(rating_scale):
        raise ValueError("rating and rating_scale must be finite")
    if rating_scale <= 0.0 or rating < 0.0 or rating > rating_scale:
        raise ValueError("rating is outside its declared source scale")
    return max(0.0, min(1.0, rating / rating_scale))


def _minimum_review_count(values: Iterable[int | None]) -> int:
    counts = sorted(value for value in values if value is not None)
    if not counts:
        return 1
    # Nearest-rank percentile; it is deterministic for both odd and even
    # fixture sets and reacts to the current dataset rather than a source name.
    rank = max(1, math.ceil(len(counts) * MINIMUM_CONFIDENCE_REVIEW_COUNT_PERCENTILE))
    return max(1, counts[rank - 1])


def _category_context(
    records: Iterable[EntitySourceRecord],
) -> tuple[dict[str, float], dict[str, int]]:
    ratings_by_category: dict[str, list[float]] = {}
    counts_by_category: dict[str, list[int | None]] = {}
    for record in records:
        try:
            normalized = normalize_rating(record.rating, record.rating_scale)
        except ValueError:
            continue
        category = normalize_text(record.category)
        if normalized is not None:
            ratings_by_category.setdefault(category, []).append(normalized)
        counts_by_category.setdefault(category, []).append(record.review_count)
    priors = {category: median(values) for category, values in ratings_by_category.items() if values}
    minimums = {
        category: _minimum_review_count(values)
        for category, values in counts_by_category.items()
    }
    return priors, minimums


def category_rating_context(
    records: Iterable[EntitySourceRecord],
) -> tuple[dict[str, float], dict[str, int]]:
    """Build priors and ``m`` from the complete input batch, by category."""

    return _category_context(records)


def _freshness(record: EntitySourceRecord, now: datetime) -> float:
    fetched_at = record.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    reference = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (reference - fetched_at).total_seconds() / 86_400.0)
    return max(0.0, min(1.0, math.exp(-age_days / RATING_FRESHNESS_HALF_LIFE_DAYS)))


def source_confidence(record: EntitySourceRecord, *, now: datetime) -> float:
    """Score record quality from observable fields, never source reputation."""

    available = 3  # source, name, category are mandatory at this boundary.
    total = 9
    if record.source_id:
        available += 1
    if record.source_url:
        available += 1
    if record.address:
        available += 1
    if record.lat is not None and record.lng is not None:
        available += 1
    if record.rating is not None and record.rating_scale is not None:
        available += 1
    if record.review_count is not None:
        available += 1
    completeness = min(1.0, available / total)

    # Structured identity and location fields are observable evidence of a
    # well-formed source row, not a hard-coded opinion about the source.
    structured_fields = sum(
        (
            bool(record.source_id),
            bool(record.source_url),
            bool(record.address),
            record.lat is not None and record.lng is not None,
        )
    )
    structured = structured_fields / 4.0
    freshness = _freshness(record, now)
    return max(0.0, min(1.0, 0.45 * completeness + 0.25 * structured + 0.30 * freshness))


def build_rating_profile(
    records: Iterable[EntitySourceRecord],
    *,
    source_record_ids: dict[int, str] | None = None,
    priors: dict[str, float] | None = None,
    minimums: dict[str, int] | None = None,
) -> RatingProfile:
    """Aggregate one canonical cluster using a dataset-derived Bayesian prior."""

    materialized = [coerce_source_record(record) for record in records]
    computed_priors, computed_minimums = _category_context(materialized)
    priors = computed_priors if priors is None else priors
    minimums = computed_minimums if minimums is None else minimums
    if not materialized:
        return RatingProfile(None, None, None, False, None, 1, ())
    category = normalize_text(materialized[0].category)
    prior = priors.get(category)
    minimum = minimums.get(category, 1)
    observations: list[RatingObservation] = []
    for index, record in enumerate(materialized):
        normalized = normalize_rating(record.rating, record.rating_scale)
        if normalized is None:
            continue
        observations.append(
            RatingObservation(
                source_record_id=(source_record_ids or {}).get(index, f"input-{index}"),
                normalized_rating=normalized,
                review_count=record.review_count,
            )
        )
    if not observations:
        return RatingProfile(None, None, None, False, prior, minimum, ())

    weighted_sum = 0.0
    total_weight = 0.0
    known_counts = [observation.review_count for observation in observations if observation.review_count is not None]
    observed_sum = sum(known_counts) if known_counts else None
    for observation in observations:
        if observation.review_count is None or prior is None:
            adjusted = observation.normalized_rating
        else:
            reviews = observation.review_count
            adjusted = (
                (reviews / (reviews + minimum)) * observation.normalized_rating
                + (minimum / (reviews + minimum)) * prior
            )
        # log1p prevents one very large source count from erasing all other
        # observations while still respecting review-count evidence.
        weight = math.log1p(observation.review_count) if observation.review_count is not None else 1.0
        weighted_sum += adjusted * max(1.0, weight)
        total_weight += max(1.0, weight)
    aggregate = weighted_sum / total_weight

    values = [observation.normalized_rating for observation in observations]
    spread = max(values) - min(values) if len(values) > 1 else 0.0
    disagreement = spread > 0.20
    if observed_sum is None:
        review_confidence = 0.20
    else:
        review_confidence = min(1.0, math.log1p(observed_sum) / math.log1p(max(10, minimum * 10)))
    coverage = len(observations) / max(1, len(materialized))
    agreement = 1.0 if len(values) == 1 else max(0.0, 1.0 - min(1.0, spread / 0.50))
    rating_confidence = max(0.0, min(1.0, 0.45 * review_confidence + 0.30 * coverage + 0.25 * agreement))
    return RatingProfile(
        normalized_rating=aggregate,
        rating_confidence=rating_confidence,
        observed_review_count_sum=observed_sum,
        rating_disagreement=disagreement,
        prior=prior,
        minimum_review_count=minimum,
        observations=tuple(observations),
    )


__all__ = [
    "MINIMUM_CONFIDENCE_REVIEW_COUNT_PERCENTILE",
    "RATING_FRESHNESS_HALF_LIFE_DAYS",
    "RatingObservation",
    "RatingProfile",
    "build_rating_profile",
    "category_rating_context",
    "normalize_rating",
    "source_confidence",
]
