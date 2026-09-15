"""V1.0 deterministic recommendation and explainability engine."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from backend.recommendations.models import (
    FEATURE_NAMES,
    RANKING_VERSION,
    ExcludedCandidate,
    RankingResult,
    Recommendation,
    RecommendationFeatureScores,
    RecommendationMode,
    RecommendationWeights,
)
from backend.recommendations.normalization import (
    RobustBounds,
    normalize_lower_is_better,
    normalize_unit,
    renormalize_weights,
    robust_bounds,
    saturating_count_score,
    weighted_sum,
)
from backend.trips.models import (
    SourceDataStatus,
    TripCandidate,
    TripCostStatus,
    TripType,
)

LOGGER = logging.getLogger(__name__)


DEFAULT_MODE_WEIGHTS: dict[RecommendationMode, dict[str, float]] = {
    RecommendationMode.BALANCED: {
        "cost": 0.30,
        "accommodation": 0.25,
        "restaurant": 0.10,
        "attraction": 0.15,
        "driving": 0.20,
    },
    RecommendationMode.LOWEST_COST: {
        "cost": 1.00,
        "accommodation": 0.00,
        "restaurant": 0.00,
        "attraction": 0.00,
        "driving": 0.00,
    },
    RecommendationMode.VALUE: {
        "cost": 0.40,
        "accommodation": 0.25,
        "restaurant": 0.10,
        "attraction": 0.15,
        "driving": 0.10,
    },
    RecommendationMode.ACCOMMODATION_QUALITY: {
        "cost": 0.15,
        "accommodation": 0.65,
        "restaurant": 0.05,
        "attraction": 0.05,
        "driving": 0.10,
    },
    RecommendationMode.SIGHTSEEING: {
        "cost": 0.10,
        "accommodation": 0.15,
        "restaurant": 0.15,
        "attraction": 0.50,
        "driving": 0.10,
    },
    RecommendationMode.LOW_DRIVING: {
        "cost": 0.15,
        "accommodation": 0.10,
        "restaurant": 0.05,
        "attraction": 0.00,
        "driving": 0.70,
    },
}

MODE_LABELS: dict[RecommendationMode, str] = {
    RecommendationMode.BALANCED: "균형형",
    RecommendationMode.LOWEST_COST: "최저비용",
    RecommendationMode.VALUE: "가성비",
    RecommendationMode.ACCOMMODATION_QUALITY: "숙소품질 우선",
    RecommendationMode.SIGHTSEEING: "관광 우선",
    RecommendationMode.LOW_DRIVING: "운전 최소",
}

MODE_REQUIRED_FEATURES: dict[RecommendationMode, tuple[str, ...]] = {
    RecommendationMode.BALANCED: (),
    RecommendationMode.LOWEST_COST: ("cost",),
    RecommendationMode.VALUE: ("cost",),
    RecommendationMode.ACCOMMODATION_QUALITY: ("accommodation",),
    RecommendationMode.SIGHTSEEING: ("attraction",),
    RecommendationMode.LOW_DRIVING: ("driving",),
}

RESTAURANT_COUNT_SCALE = 10.0
ATTRACTION_COUNT_SCALE = 12.0
DRIVING_DISTANCE_WEIGHT = 0.40
DRIVING_DURATION_WEIGHT = 0.60
RATING_CONFIDENCE_FLOOR = 0.35
RATING_CONFIDENCE_SLOPE = 0.65
RATING_FRESHNESS_HALF_LIFE_DAYS = 180.0


class RecommendationError(ValueError):
    """A ranking request cannot be safely evaluated."""


@dataclass(frozen=True)
class _FeatureSnapshot:
    raw: dict[str, float | int | None]
    normalized: dict[str, float | None]
    scores: RecommendationFeatureScores
    confidence: float
    warnings: tuple[str, ...]


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _enum_value(value: object) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _status(candidate: TripCandidate, key: str) -> str:
    value = candidate.component_statuses.get(key)
    if value is None and key.endswith("s"):
        value = candidate.component_statuses.get(key[:-1])
    status = _enum_value(value if value is not None else SourceDataStatus.UNKNOWN)
    if status in {"error", "failed", "source_error", "source_failed"}:
        return SourceDataStatus.UNAVAILABLE.value
    return status


def _cost_status(candidate: TripCandidate) -> str:
    return _enum_value(candidate.costs.status)


def _source_status_confidence(status: str) -> float:
    return {
        "ok": 1.0,
        "partial": 0.60,
        "empty": 0.82,
        "not_required": 1.0,
        "unavailable": 0.15,
        "unknown": 0.35,
    }.get(status, 0.35)


def _cost_evidence_confidence(status: str) -> float:
    return {
        TripCostStatus.VERIFIED_COMPLETE.value: 1.0,
        TripCostStatus.ESTIMATED_COMPLETE.value: 0.78,
        TripCostStatus.PARTIAL.value: 0.42,
        TripCostStatus.UNKNOWN.value: 0.20,
    }.get(status, 0.20)


def _freshness_confidence(candidate: TripCandidate, now: datetime) -> float:
    updated_at = candidate.provenance.source_updated_at
    if updated_at is None:
        return 0.50
    observed = updated_at if updated_at.tzinfo is not None else updated_at.replace(tzinfo=timezone.utc)
    reference = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (reference - observed).total_seconds() / 86_400.0)
    return max(0.0, min(1.0, math.exp(-age_days / RATING_FRESHNESS_HALF_LIFE_DAYS)))


def _canonical_confidence(candidate: TripCandidate) -> float:
    places = [
        place
        for place in [candidate.accommodation, *candidate.restaurants, *candidate.attractions]
        if place is not None
    ]
    if not places:
        return 0.25
    values = [max(0.0, min(1.0, place.confidence)) for place in places]
    return sum(values) / len(values)


def _rating_score(rating: float | None, confidence: float | None) -> float | None:
    normalized = normalize_unit(rating)
    if normalized is None:
        return None
    evidence = RATING_CONFIDENCE_FLOOR if confidence is None else normalize_unit(confidence)
    if evidence is None:
        evidence = RATING_CONFIDENCE_FLOOR
    adjustment = RATING_CONFIDENCE_FLOOR + RATING_CONFIDENCE_SLOPE * evidence
    return max(0.0, min(1.0, normalized * adjustment))


def _place_score(
    *,
    count: int,
    rating_mean: float | None,
    status: str,
    count_scale: float,
) -> float | None:
    """Score valid availability separately from source failure."""

    if status in {"unavailable", "unknown"}:
        return None
    if status == "partial" and count == 0 and rating_mean is None:
        return None
    availability = saturating_count_score(count, scale=count_scale)
    assert availability is not None
    quality = normalize_unit(rating_mean)
    if quality is None:
        return availability
    # Availability and quality are both bounded.  A count of zero remains a
    # valid zero only for a source that explicitly reported an empty result.
    return max(0.0, min(1.0, 0.60 * availability + 0.40 * quality))


def _feature_bounds(candidates: list[TripCandidate]) -> tuple[RobustBounds | None, RobustBounds | None, RobustBounds | None]:
    return (
        robust_bounds(
            candidate.costs.total_krw
            for candidate in candidates
            if candidate.costs.total_krw is not None
        ),
        robust_bounds(
            candidate.quality.driving_distance_km
            for candidate in candidates
            if candidate.quality.driving_distance_km is not None
        ),
        robust_bounds(
            candidate.quality.driving_duration_min
            for candidate in candidates
            if candidate.quality.driving_duration_min is not None
        ),
    )


def _driving_score(
    candidate: TripCandidate,
    distance_bounds: RobustBounds | None,
    duration_bounds: RobustBounds | None,
) -> float | None:
    distance = normalize_lower_is_better(candidate.quality.driving_distance_km, distance_bounds)
    duration = normalize_lower_is_better(candidate.quality.driving_duration_min, duration_bounds)
    values: list[tuple[float, float]] = []
    if distance is not None:
        values.append((distance, DRIVING_DISTANCE_WEIGHT))
    if duration is not None:
        values.append((duration, DRIVING_DURATION_WEIGHT))
    if not values:
        return None
    total_weight = sum(weight for _value, weight in values)
    return max(0.0, min(1.0, sum(value * weight for value, weight in values) / total_weight))


def _snapshot(
    candidate: TripCandidate,
    *,
    cost_bounds: RobustBounds | None,
    distance_bounds: RobustBounds | None,
    duration_bounds: RobustBounds | None,
    now: datetime,
) -> _FeatureSnapshot:
    quality = candidate.quality
    cost_value = candidate.costs.total_krw if candidate.costs.complete else None
    accommodation_value = _rating_score(
        quality.accommodation_rating,
        quality.accommodation_rating_confidence,
    )
    restaurant_value = _place_score(
        count=quality.nearby_restaurant_count,
        rating_mean=quality.restaurant_rating_mean,
        status=_status(candidate, "restaurants"),
        count_scale=RESTAURANT_COUNT_SCALE,
    )
    attraction_value = _place_score(
        count=quality.nearby_attraction_count,
        rating_mean=quality.attraction_rating_mean,
        status=_status(candidate, "attractions"),
        count_scale=ATTRACTION_COUNT_SCALE,
    )
    driving_value = _driving_score(candidate, distance_bounds, duration_bounds)
    cost_value_score = normalize_lower_is_better(cost_value, cost_bounds)
    normalized = {
        "cost": cost_value_score,
        "accommodation": accommodation_value,
        "restaurant": restaurant_value,
        "attraction": attraction_value,
        "driving": driving_value,
    }
    raw: dict[str, float | int | None] = {
        "cost": cost_value,
        "accommodation": quality.accommodation_rating,
        "accommodation_rating_confidence": quality.accommodation_rating_confidence,
        "restaurant": quality.nearby_restaurant_count,
        "restaurant_count": quality.nearby_restaurant_count,
        "restaurant_rating_mean": quality.restaurant_rating_mean,
        "attraction": quality.nearby_attraction_count,
        "attraction_count": quality.nearby_attraction_count,
        "attraction_rating_mean": quality.attraction_rating_mean,
        "driving_distance_km": quality.driving_distance_km,
        "driving_duration_min": quality.driving_duration_min,
    }
    source_statuses = [
        _status(candidate, "driving"),
        _status(candidate, "restaurants"),
        _status(candidate, "attractions"),
    ]
    if candidate.trip_type is TripType.OVERNIGHT:
        source_statuses.append(_status(candidate, "accommodation"))
    source_evidence = sum(_source_status_confidence(status) for status in source_statuses) / len(source_statuses)
    if candidate.trip_type is TripType.DAY_TRIP:
        rating_evidence = 0.85
    elif quality.accommodation_rating is None:
        rating_evidence = 0.25
    else:
        rating_evidence = quality.accommodation_rating_confidence or 0.25
    linked_places = bool(
        candidate.accommodation or candidate.restaurants or candidate.attractions
    )
    completeness = quality.place_data_completeness if linked_places else 0.25
    place_evidence = max(0.0, min(1.0, 0.50 * _canonical_confidence(candidate) + 0.50 * completeness))
    confidence = max(
        0.0,
        min(
            1.0,
            0.30 * _cost_evidence_confidence(_cost_status(candidate))
            + 0.25 * source_evidence
            + 0.20 * place_evidence
            + 0.15 * max(0.0, min(1.0, rating_evidence))
            + 0.10 * _freshness_confidence(candidate, now),
        ),
    )
    warnings = list(candidate.warnings)
    if _cost_status(candidate) == TripCostStatus.ESTIMATED_COMPLETE.value:
        warnings.append("비용에 추정값이 포함되어 있습니다.")
    if _cost_status(candidate) in {TripCostStatus.PARTIAL.value, TripCostStatus.UNKNOWN.value}:
        warnings.append("총비용이 완전하지 않아 비용 비교에 제한이 있습니다.")
    for key, label in (
        ("accommodation", "숙소"),
        ("restaurants", "음식점"),
        ("attractions", "관광지"),
        ("driving", "운전 비용"),
    ):
        if _status(candidate, key) == SourceDataStatus.UNAVAILABLE.value:
            warnings.append(f"{label} 데이터를 확인하지 못했습니다.")
    if quality.accommodation_rating is not None and (quality.accommodation_rating_confidence or 0.0) < 0.5:
        warnings.append("숙소 평점 데이터 신뢰도가 낮습니다.")
    if completeness < 0.5:
        warnings.append("장소 데이터가 부족합니다.")
    if confidence < 0.5:
        warnings.append("추천 데이터 신뢰도가 낮습니다.")
    return _FeatureSnapshot(
        raw=raw,
        normalized=normalized,
        scores=RecommendationFeatureScores(
            cost_score=cost_value_score,
            accommodation_score=accommodation_value,
            restaurant_score=restaurant_value,
            attraction_score=attraction_value,
            driving_score=driving_value,
            data_confidence_score=confidence,
        ),
        confidence=confidence,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _resolved_weights(
    mode: RecommendationMode,
    custom_weights: RecommendationWeights | Mapping[str, float] | None,
) -> dict[str, float]:
    if custom_weights is None:
        return dict(DEFAULT_MODE_WEIGHTS[mode])
    try:
        model = (
            custom_weights
            if isinstance(custom_weights, RecommendationWeights)
            else RecommendationWeights.model_validate(custom_weights)
        )
    except Exception as error:  # Pydantic provides the public validation details.
        raise RecommendationError(f"invalid custom recommendation weights: {error}") from error
    return model.normalized()


def _eligibility(
    candidate: TripCandidate,
    mode: RecommendationMode,
    normalized: Mapping[str, float | None],
    weights: Mapping[str, float],
) -> list[str]:
    reasons: list[str] = []
    for feature in MODE_REQUIRED_FEATURES[mode]:
        if normalized.get(feature) is None:
            reasons.append(
                {
                    "cost": "TOTAL_COST_REQUIRED",
                    "accommodation": "ACCOMMODATION_RATING_REQUIRED",
                    "attraction": "ATTRACTION_DATA_REQUIRED",
                    "driving": "ROUTE_DISTANCE_DURATION_REQUIRED",
                }[feature]
            )
    if not any(normalized.get(feature) is not None and weights.get(feature, 0.0) > 0 for feature in FEATURE_NAMES):
        reasons.append("NO_AVAILABLE_FEATURES")
    # These reasons are deliberately additive.  A caller can tell whether a
    # candidate was excluded for a mode's contract or simply lacked all data.
    if (
        mode in {RecommendationMode.LOWEST_COST, RecommendationMode.VALUE}
        and candidate.costs.total_krw is None
        and "TOTAL_COST_REQUIRED" not in reasons
    ):
        reasons.append("TOTAL_COST_REQUIRED")
    return list(dict.fromkeys(reasons))


def _won(value: int | None) -> str:
    return "확인 불가" if value is None else f"{value:,}원"


def _explain(
    candidate: TripCandidate,
    mode: RecommendationMode,
    snapshot: _FeatureSnapshot,
    active_weights: Mapping[str, float],
) -> tuple[list[str], list[str], list[str], str]:
    values = snapshot.normalized
    strengths: list[str] = []
    weaknesses: list[str] = []
    warnings = list(snapshot.warnings)

    if active_weights.get("cost", 0.0) > 0:
        if values["cost"] is not None and values["cost"] >= 0.65:
            strengths.append(f"총비용 {_won(candidate.costs.total_krw)}이 후보 중 낮은 편입니다.")
        elif values["cost"] is not None and values["cost"] <= 0.35:
            weaknesses.append(f"총비용 {_won(candidate.costs.total_krw)}이 후보 중 높은 편입니다.")
        elif values["cost"] is None:
            weaknesses.append("총비용을 확인할 수 없습니다.")

    if active_weights.get("accommodation", 0.0) > 0:
        if values["accommodation"] is None:
            weaknesses.append("숙소 평점을 확인할 수 없습니다.")
        elif values["accommodation"] >= 0.65:
            strengths.append("숙소 평점과 평점 신뢰도가 비교적 높습니다.")
        elif values["accommodation"] <= 0.35:
            weaknesses.append("숙소 평점 또는 평점 신뢰도가 낮은 편입니다.")

    for feature, label in (("restaurant", "음식점"), ("attraction", "관광지"), ("driving", "운전 부담")):
        if active_weights.get(feature, 0.0) <= 0:
            continue
        value = values[feature]
        if value is None:
            if feature == "driving":
                weaknesses.append("운전 거리·시간을 확인할 수 없습니다.")
            elif _status(candidate, f"{feature}s") == SourceDataStatus.UNAVAILABLE.value:
                warnings.append(f"주변 {label} 데이터 수집 실패는 품질 부족으로 해석하지 않았습니다.")
            else:
                weaknesses.append(f"주변 {label} 데이터를 확인할 수 없습니다.")
        elif value >= 0.65:
            if feature == "driving":
                strengths.append("운전 거리·시간 부담이 비교적 낮습니다.")
            else:
                strengths.append(f"주변 {label} 선택지와 품질이 비교적 좋습니다.")
        elif value <= 0.35:
            if feature == "driving":
                weaknesses.append("운전 거리·시간 부담이 큰 편입니다.")
            elif _status(candidate, f"{feature}s") == SourceDataStatus.EMPTY.value:
                weaknesses.append(f"검색된 주변 {label}가 많지 않습니다.")
            else:
                weaknesses.append(f"주변 {label} 선택지와 품질이 낮은 편입니다.")

    if not strengths:
        strengths.append("확인 가능한 비교 근거를 바탕으로 계산했습니다.")
    if snapshot.confidence < 0.5:
        warnings.append("데이터 신뢰도가 낮아 순위를 보수적으로 해석해야 합니다.")
    strengths = list(dict.fromkeys(strengths))[:5]
    weaknesses = list(dict.fromkeys(weaknesses))[:5]
    warnings = list(dict.fromkeys(warnings))[:10]
    parts = [f"{MODE_LABELS[mode]} 기준으로 계산했습니다."]
    if strengths:
        parts.append("강점: " + " ".join(strengths[:2]))
    if weaknesses:
        parts.append("주의: " + " ".join(weaknesses[:2]))
    if warnings:
        parts.append("데이터 주의: " + " ".join(warnings[:2]))
    return strengths, weaknesses, warnings, " ".join(parts)


def _candidate_set_fingerprint(candidates: list[TripCandidate]) -> str:
    payload = []
    for candidate in sorted(candidates, key=lambda item: item.id):
        data = candidate.model_dump(mode="json")
        data.pop("created_at", None)
        payload.append(data)
    return _fingerprint(payload)


def _sort_key(recommendation: Recommendation, candidate_by_id: Mapping[str, TripCandidate]) -> tuple[float, float, float, float, str]:
    candidate = candidate_by_id[recommendation.candidate_id]
    verified_total = (
        candidate.costs.total_krw
        if _cost_status(candidate) == TripCostStatus.VERIFIED_COMPLETE.value
        else math.inf
    )
    duration = candidate.quality.driving_duration_min
    return (
        -recommendation.final_score,
        -recommendation.confidence,
        float(verified_total),
        float(duration) if duration is not None else math.inf,
        recommendation.candidate_id,
    )


class RecommendationService:
    """Rank already assembled candidates without any external I/O."""

    version = RANKING_VERSION

    def rank(
        self,
        candidates: Iterable[TripCandidate | Mapping[str, Any]],
        mode: RecommendationMode | str = RecommendationMode.BALANCED,
        *,
        custom_weights: RecommendationWeights | Mapping[str, float] | None = None,
        limit: int = 10,
        now: datetime | None = None,
    ) -> RankingResult:
        try:
            mode_value = mode if isinstance(mode, RecommendationMode) else RecommendationMode(mode)
        except ValueError as error:
            raise RecommendationError(f"unsupported recommendation mode: {mode}") from error
        if limit < 1 or limit > 200:
            raise RecommendationError("recommendation limit must be between 1 and 200")
        materialized = [
            candidate
            if isinstance(candidate, TripCandidate)
            else TripCandidate.model_validate(candidate)
            for candidate in candidates
        ]
        candidate_ids = [candidate.id for candidate in materialized]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise RecommendationError("candidate IDs must be unique")
        if len(materialized) > 200:
            raise RecommendationError("at most 200 candidates can be ranked")
        resolved_weights = _resolved_weights(mode_value, custom_weights)
        weight_model = RecommendationWeights.model_validate(resolved_weights)
        reference_time = now or datetime.now(timezone.utc)
        fingerprint = _candidate_set_fingerprint(materialized)
        cost_bounds, distance_bounds, duration_bounds = _feature_bounds(materialized)
        snapshots = {
            candidate.id: _snapshot(
                candidate,
                cost_bounds=cost_bounds,
                distance_bounds=distance_bounds,
                duration_bounds=duration_bounds,
                now=reference_time,
            )
            for candidate in materialized
        }
        eligible_rows: list[tuple[TripCandidate, _FeatureSnapshot, dict[str, float], float]] = []
        excluded: list[ExcludedCandidate] = []
        for candidate in materialized:
            snapshot = snapshots[candidate.id]
            available = [
                feature for feature in FEATURE_NAMES if snapshot.normalized.get(feature) is not None
            ]
            active_weights = renormalize_weights(resolved_weights, available)
            reasons = _eligibility(candidate, mode_value, snapshot.normalized, resolved_weights)
            if not active_weights and "NO_AVAILABLE_FEATURES" not in reasons:
                reasons.append("NO_AVAILABLE_FEATURES")
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        candidate_id=candidate.id,
                        reasons=list(dict.fromkeys(reasons)),
                    )
                )
                continue
            final_score = weighted_sum(
                {name: value for name, value in snapshot.normalized.items() if value is not None},
                active_weights,
            )
            eligible_rows.append((candidate, snapshot, active_weights, final_score))

        candidate_by_id = {candidate.id: candidate for candidate in materialized}
        recommendations: list[Recommendation] = []
        for candidate, snapshot, active_weights, final_score in eligible_rows:
            strengths, weaknesses, warnings, explanation = _explain(
                candidate,
                mode_value,
                snapshot,
                active_weights,
            )
            recommendations.append(
                Recommendation(
                    rank=1,
                    candidate_id=candidate.id,
                    recommendation_mode=mode_value,
                    final_score=round(final_score, 12),
                    feature_scores=snapshot.scores,
                    confidence=round(snapshot.confidence, 12),
                    strengths=strengths,
                    weaknesses=weaknesses,
                    warnings=warnings,
                    eligible=True,
                    explanation=explanation,
                    raw_features=snapshot.raw,
                    normalized_features={
                        **snapshot.normalized,
                        "data_confidence": snapshot.scores.data_confidence_score,
                    },
                    weighted_contributions={
                        name: round(snapshot.normalized[name] * weight, 12)
                        for name, weight in active_weights.items()
                        if snapshot.normalized.get(name) is not None
                    },
                )
            )
        recommendations.sort(key=lambda item: _sort_key(item, candidate_by_id))
        recommendations = [
            recommendation.model_copy(update={"rank": index})
            for index, recommendation in enumerate(recommendations[:limit], start=1)
        ]
        excluded.sort(key=lambda item: item.candidate_id)
        warnings: list[str] = []
        if excluded:
            warnings.append(f"{len(excluded)}개 후보는 선택한 추천 모드의 필수 데이터 부족으로 제외되었습니다.")
        LOGGER.debug(
            "recommendation ranking candidates=%d eligible=%d excluded=%d mode=%s",
            len(materialized),
            len(eligible_rows),
            len(excluded),
            mode_value.value,
        )
        return RankingResult(
            mode=mode_value,
            ranking_version=self.version,
            recommendations=recommendations,
            excluded_candidates=excluded,
            candidate_count=len(materialized),
            eligible_count=len(eligible_rows),
            candidate_set_fingerprint=fingerprint,
            weights=weight_model,
            warnings=warnings,
            generated_at=reference_time,
        )


__all__ = [
    "ATTRACTION_COUNT_SCALE",
    "DEFAULT_MODE_WEIGHTS",
    "MODE_LABELS",
    "MODE_REQUIRED_FEATURES",
    "RESTAURANT_COUNT_SCALE",
    "RecommendationError",
    "RecommendationService",
]
