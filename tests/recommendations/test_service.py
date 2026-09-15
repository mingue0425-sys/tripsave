from datetime import datetime, timezone

import pytest

from backend.recommendations import RecommendationMode, RecommendationService
from backend.trips.models import CostComponentStatus, SourceDataStatus, TripCostStatus
from backend.trips.service import TripCandidateService
from tests.trips.helpers import make_offer, make_place, make_request

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def candidate(
    candidate_id: str,
    *,
    price: int | None = 240_000,
    rating: float | None = 0.90,
    rating_confidence: float | None = 0.90,
    source_statuses: dict[str, SourceDataStatus] | None = None,
    with_places: bool = True,
):
    hotel = make_place(
        candidate_id,
        f"Hotel {candidate_id}",
        "accommodation",
        source="booking",
        source_id=f"hotel-{candidate_id}",
        rating=rating,
        rating_confidence=rating_confidence,
    )
    places = [hotel] if with_places else []
    offers = [
        make_offer(
            source_id=f"hotel-{candidate_id}",
            source_offer_id=f"offer-{candidate_id}",
            final_price_krw=price,
            price_freshness="fresh" if price is not None else "unknown",
        )
    ]
    request = make_request(
        canonical_places=places,
        offers=offers,
        source_statuses=source_statuses or {},
    )
    response = TripCandidateService().assemble(request, now=NOW)
    return response.candidates[0]


def test_all_preset_modes_have_deterministic_normalized_scores() -> None:
    candidates = [
        candidate("a", price=200_000, rating=0.82),
        candidate("b", price=300_000, rating=0.95),
        candidate("c", price=400_000, rating=0.88),
    ]
    service = RecommendationService()

    for mode in RecommendationMode:
        result = service.rank(candidates, mode, now=NOW)
        assert result.ranking_version == "v1.0.0"
        assert result.candidate_count == 3
        assert result.recommendations
        assert all(0.0 <= item.final_score <= 1.0 for item in result.recommendations)
        assert all(0.0 <= item.confidence <= 1.0 for item in result.recommendations)
        assert all(sum(item.weighted_contributions.values()) == pytest.approx(item.final_score) for item in result.recommendations)


def test_lowest_cost_uses_total_only_and_never_partial_subtotal() -> None:
    cheap = candidate("cheap", price=200_000)
    complete = candidate("complete", price=300_000)
    partial = candidate("partial", price=None, with_places=False)

    result = RecommendationService().rank(
        [partial, complete, cheap],
        RecommendationMode.LOWEST_COST,
        now=NOW,
    )

    assert [item.candidate_id for item in result.recommendations] == [cheap.id, complete.id]
    assert result.excluded_candidates[0].candidate_id == partial.id
    assert "TOTAL_COST_REQUIRED" in result.excluded_candidates[0].reasons
    assert partial.costs.known_subtotal_krw == 44_000


def test_value_excludes_partial_cost_but_balanced_can_keep_it_with_warning() -> None:
    partial = candidate("partial-value", price=None)
    complete = candidate("complete-value", price=300_000)

    value = RecommendationService().rank(
        [partial, complete], RecommendationMode.VALUE, now=NOW
    )
    balanced = RecommendationService().rank(
        [partial, complete], RecommendationMode.BALANCED, now=NOW
    )

    assert [item.candidate_id for item in value.recommendations] == [complete.id]
    assert partial.id in {item.candidate_id for item in value.excluded_candidates}
    assert partial.id in {item.candidate_id for item in balanced.recommendations}
    partial_recommendation = next(
        item for item in balanced.recommendations if item.candidate_id == partial.id
    )
    assert "총비용이 완전하지 않아 비용 비교에 제한이 있습니다." in partial_recommendation.warnings


def test_accommodation_quality_can_rank_without_a_known_total() -> None:
    partial = candidate("quality-without-price", price=None, rating=0.95)

    result = RecommendationService().rank(
        [partial], RecommendationMode.ACCOMMODATION_QUALITY, now=NOW
    )

    assert result.recommendations[0].candidate_id == partial.id
    assert partial.costs.total_krw is None


def test_balanced_renormalizes_missing_feature_and_source_failure_is_not_zero() -> None:
    hotel = candidate(
        "hotel",
        source_statuses={"restaurants": SourceDataStatus.UNAVAILABLE},
    )

    result = RecommendationService().rank([hotel], RecommendationMode.BALANCED, now=NOW)
    item = result.recommendations[0]

    assert item.feature_scores.restaurant_score is None
    assert item.normalized_features["restaurant"] is None
    assert "음식점 데이터를 확인하지 못했습니다." in item.warnings
    assert item.final_score > 0.0
    assert sum(item.weighted_contributions.values()) == pytest.approx(item.final_score)


def test_low_review_high_rating_does_not_beat_high_confidence_rating() -> None:
    low_review = candidate("low", rating=0.99, rating_confidence=0.20)
    established = candidate("established", rating=0.92, rating_confidence=0.95)

    result = RecommendationService().rank(
        [low_review, established],
        RecommendationMode.ACCOMMODATION_QUALITY,
        now=NOW,
    )

    assert result.recommendations[0].candidate_id == established.id
    assert result.recommendations[0].feature_scores.accommodation_score > result.recommendations[1].feature_scores.accommodation_score


def test_estimated_cost_is_rankable_but_less_confident_than_verified() -> None:
    verified = candidate("verified", price=240_000)
    estimated = candidate("estimated", price=240_000)
    estimated = estimated.model_copy(
        update={
            "costs": estimated.costs.model_copy(
                update={
                    "status": TripCostStatus.ESTIMATED_COMPLETE,
                    "estimated_components": ["accommodation"],
                    "components": [
                        estimated.costs.components[0],
                        estimated.costs.components[1].model_copy(update={"status": CostComponentStatus.ESTIMATED}),
                    ],
                }
            )
        }
    )

    result = RecommendationService().rank(
        [estimated, verified],
        RecommendationMode.LOWEST_COST,
        now=NOW,
    )

    by_id = {item.candidate_id: item for item in result.recommendations}
    assert set(by_id) == {estimated.id, verified.id}
    assert by_id[verified.id].confidence > by_id[estimated.id].confidence
    assert "비용에 추정값이 포함되어 있습니다." in by_id[estimated.id].warnings


def test_tie_break_prefers_confidence_then_deterministic_id() -> None:
    high = candidate("high-confidence")
    low = candidate("low-confidence")
    high_data = high.model_dump(mode="json")
    low_data = low.model_dump(mode="json")
    high_data["accommodation"]["confidence"] = 0.99
    low_data["accommodation"]["confidence"] = 0.20
    high = type(high).model_validate(high_data)
    low = type(low).model_validate(low_data)

    result = RecommendationService().rank(
        [low, high], RecommendationMode.LOWEST_COST, now=NOW
    )

    assert result.recommendations[0].candidate_id == high.id


def test_custom_weights_normalize_and_input_order_does_not_change_ranking() -> None:
    candidates = [
        candidate("a", price=200_000),
        candidate("b", price=300_000),
        candidate("c", price=400_000),
    ]
    service = RecommendationService()
    first = service.rank(
        candidates,
        RecommendationMode.BALANCED,
        custom_weights={"cost": 0.5},
        now=NOW,
    )
    second = service.rank(
        list(reversed(candidates)),
        RecommendationMode.BALANCED,
        custom_weights={"cost": 0.5},
        now=NOW,
    )

    assert [item.candidate_id for item in first.recommendations] == [item.candidate_id for item in second.recommendations]
    assert first.candidate_set_fingerprint == second.candidate_set_fingerprint
    assert first.weights.cost == 1.0
    assert sum(first.weights.model_dump().values()) == pytest.approx(1.0)


def test_no_eligible_candidate_returns_explicit_empty_result() -> None:
    partial = candidate("partial", price=None, with_places=False)

    result = RecommendationService().rank(
        [partial],
        RecommendationMode.LOWEST_COST,
        now=NOW,
    )

    assert result.recommendations == []
    assert result.eligible_count == 0
    assert result.excluded_candidates[0].candidate_id == partial.id


def test_invalid_custom_weights_are_rejected() -> None:
    with pytest.raises(ValueError):
        RecommendationService().rank(
            [candidate("a")],
            custom_weights={"cost": -1},
            now=NOW,
        )
    with pytest.raises(ValueError):
        RecommendationService().rank(
            [candidate("a")],
            custom_weights={"unknown": 1},
            now=NOW,
        )
