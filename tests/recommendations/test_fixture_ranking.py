from datetime import datetime, timezone

from backend.recommendations import RecommendationMode, RecommendationService
from backend.trips.models import SourceDataStatus, TripCandidate
from backend.trips.service import TripCandidateService
from tests.trips.helpers import make_offer, make_place, make_request

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def fixture_candidate(
    name: str,
    *,
    price: int,
    rating: float,
    restaurants: int,
    attractions: int,
    distance_km: float,
    duration_min: float,
) -> TripCandidate:
    hotel = make_place(
        f"hotel-{name}",
        f"Fixture {name}",
        "accommodation",
        source="booking",
        source_id=f"hotel-{name}",
        rating=rating,
        rating_confidence=0.9,
    )
    assembled = TripCandidateService().assemble(
        make_request(
            canonical_places=[hotel],
            offers=[
                make_offer(
                    source_id=f"hotel-{name}",
                    source_offer_id=f"offer-{name}",
                    final_price_krw=price,
                )
            ],
            source_statuses={
                "restaurants": SourceDataStatus.OK,
                "attractions": SourceDataStatus.OK,
            },
        ),
        now=NOW,
    )
    data = assembled.candidates[0].model_dump(mode="json")
    data["id"] = f"fixture-{name}"
    data["quality"].update(
        {
            "nearby_restaurant_count": restaurants,
            "nearby_attraction_count": attractions,
            "restaurant_rating_mean": 0.85 if restaurants else None,
            "attraction_rating_mean": 0.90 if attractions else None,
            "driving_distance_km": distance_km,
            "driving_duration_min": duration_min,
            "place_data_completeness": 0.9,
        }
    )
    return TripCandidate.model_validate(data)


def fixture_set() -> list[TripCandidate]:
    return [
        fixture_candidate("cheap", price=160_000, rating=0.65, restaurants=2, attractions=2, distance_km=600, duration_min=360),
        fixture_candidate("balanced", price=260_000, rating=0.90, restaurants=8, attractions=8, distance_km=400, duration_min=240),
        fixture_candidate("quality", price=600_000, rating=0.99, restaurants=10, attractions=10, distance_km=500, duration_min=300),
        fixture_candidate("sightseeing", price=420_000, rating=0.86, restaurants=12, attractions=25, distance_km=450, duration_min=280),
        fixture_candidate("drive", price=500_000, rating=0.84, restaurants=5, attractions=4, distance_km=100, duration_min=60),
        fixture_candidate("mid", price=330_000, rating=0.88, restaurants=6, attractions=12, distance_km=350, duration_min=210),
        fixture_candidate("bad", price=900_000, rating=0.60, restaurants=1, attractions=1, distance_km=800, duration_min=500),
        fixture_candidate("budgetquality", price=210_000, rating=0.87, restaurants=5, attractions=5, distance_km=550, duration_min=330),
        fixture_candidate("tourcheap", price=300_000, rating=0.83, restaurants=7, attractions=20, distance_km=500, duration_min=300),
        fixture_candidate("comfortable", price=380_000, rating=0.91, restaurants=8, attractions=8, distance_km=250, duration_min=150),
    ]


def test_fixture_truth_for_all_modes() -> None:
    candidates = fixture_set()
    service = RecommendationService()
    expected = {
        RecommendationMode.BALANCED: ["fixture-balanced", "fixture-comfortable", "fixture-mid"],
        RecommendationMode.LOWEST_COST: ["fixture-cheap", "fixture-budgetquality", "fixture-balanced"],
        RecommendationMode.VALUE: ["fixture-balanced", "fixture-budgetquality", "fixture-tourcheap"],
        RecommendationMode.ACCOMMODATION_QUALITY: ["fixture-comfortable", "fixture-balanced", "fixture-mid"],
        RecommendationMode.SIGHTSEEING: ["fixture-sightseeing", "fixture-tourcheap", "fixture-mid"],
        RecommendationMode.LOW_DRIVING: ["fixture-drive", "fixture-comfortable", "fixture-mid"],
    }
    for mode, expected_ids in expected.items():
        result = service.rank(candidates, mode, limit=3, now=NOW)
        assert [item.candidate_id for item in result.recommendations] == expected_ids


def test_cost_unit_scaling_keeps_relative_ranking() -> None:
    candidates = fixture_set()[:3]
    scaled = []
    for candidate in candidates:
        data = candidate.model_dump(mode="json")
        data["costs"]["driving_krw"] *= 1000
        data["costs"]["accommodation_krw"] *= 1000
        data["costs"]["known_subtotal_krw"] *= 1000
        data["costs"]["total_krw"] *= 1000
        for component in data["costs"]["components"]:
            if component["amount_krw"] is not None:
                component["amount_krw"] *= 1000
        scaled.append(TripCandidate.model_validate(data))

    service = RecommendationService()
    original = service.rank(candidates, RecommendationMode.LOWEST_COST, now=NOW)
    converted = service.rank(scaled, RecommendationMode.LOWEST_COST, now=NOW)
    assert [item.candidate_id for item in original.recommendations] == [item.candidate_id for item in converted.recommendations]
    assert [item.final_score for item in original.recommendations] == [item.final_score for item in converted.recommendations]


def test_explanations_are_consistent_with_weighted_contributions() -> None:
    result = RecommendationService().rank(fixture_set(), RecommendationMode.SIGHTSEEING, now=NOW)
    recommendation = result.recommendations[0]

    assert "관광" in recommendation.explanation or "관광지" in recommendation.explanation
    assert sum(recommendation.weighted_contributions.values()) == round(recommendation.final_score, 12)
    assert recommendation.normalized_features["data_confidence"] == recommendation.feature_scores.data_confidence_score
