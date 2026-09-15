from datetime import datetime, timezone

import pytest

from backend.accommodation.models import AccommodationPriceBasis
from backend.trips.models import (
    CostComponentStatus,
    SourceDataStatus,
    TripCostStatus,
    TripType,
    VehicleProfile,
)
from backend.trips.service import (
    TripAssemblyError,
    TripCandidateService,
    TripCostEngine,
    calculate_accommodation_cost,
)
from tests.trips.helpers import make_driving_cost, make_offer, make_place, make_request

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def test_vehicle_profile_accepts_spec_alias_and_internal_name() -> None:
    alias = VehicleProfile.model_validate({"efficiency_km_per_l": 13.5})
    internal = VehicleProfile.model_validate({"fuel_efficiency_km_per_l": 13.5})

    assert alias.fuel_efficiency_km_per_l == internal.fuel_efficiency_km_per_l == 13.5


def test_accommodation_total_stay_and_per_night_basis() -> None:
    total_stay = calculate_accommodation_cost(
        make_offer(final_price_krw=240_000, price_basis=AccommodationPriceBasis.TOTAL_STAY),
        nights=2,
        now=NOW,
    )
    per_night = calculate_accommodation_cost(
        make_offer(final_price_krw=120_000, price_basis=AccommodationPriceBasis.PER_NIGHT),
        nights=2,
        now=NOW,
    )

    assert total_stay.amount_krw == 240_000
    assert per_night.amount_krw == 240_000
    assert total_stay.status is CostComponentStatus.VERIFIED
    assert per_night.status is CostComponentStatus.VERIFIED


def test_unknown_basis_and_unknown_taxes_never_become_a_stay_total() -> None:
    unknown_basis = calculate_accommodation_cost(
        make_offer(price_basis=AccommodationPriceBasis.UNKNOWN),
        nights=2,
        now=NOW,
    )
    unknown_taxes = calculate_accommodation_cost(
        make_offer(
            final_price_krw=None,
            base_price_krw=100_000,
            taxes_krw=None,
            price_freshness="unknown",
        ),
        nights=2,
        now=NOW,
    )

    assert unknown_basis.amount_krw is None
    assert unknown_basis.reason == "ACCOMMODATION_PRICE_BASIS_UNKNOWN"
    assert unknown_taxes.amount_krw is None
    assert unknown_taxes.reason == "ACCOMMODATION_TAXES_UNKNOWN"


def test_stale_accommodation_and_estimated_driving_propagate_estimate_state() -> None:
    engine = TripCostEngine()
    result = engine.calculate_with_diagnostics(
        driving_cost=make_driving_cost(estimated=True),
        accommodation_offer=make_offer(price_freshness="stale"),
        nights=2,
        trip_type=TripType.OVERNIGHT,
        now=NOW,
    )

    assert result.breakdown.total_krw == 284_000
    assert result.breakdown.status is TripCostStatus.ESTIMATED_COMPLETE
    assert result.breakdown.estimated_components == ["driving", "accommodation"]
    assert result.breakdown.complete is True
    assert "ACCOMMODATION_OFFER_STALE" in result.warnings


def test_missing_accommodation_keeps_driving_known_subtotal_and_total_unknown() -> None:
    result = TripCostEngine().calculate_with_diagnostics(
        driving_cost=make_driving_cost(total=130_000),
        accommodation_offer=None,
        nights=2,
        trip_type=TripType.OVERNIGHT,
        now=NOW,
    )

    assert result.breakdown.driving_krw == 130_000
    assert result.breakdown.accommodation_krw is None
    assert result.breakdown.known_subtotal_krw == 130_000
    assert result.breakdown.total_krw is None
    assert result.breakdown.complete is False
    assert result.breakdown.status is TripCostStatus.PARTIAL


def test_both_required_costs_unknown_has_unknown_status() -> None:
    result = TripCostEngine().calculate_with_diagnostics(
        driving_cost=None,
        accommodation_offer=None,
        nights=2,
        trip_type=TripType.OVERNIGHT,
        now=NOW,
    )

    assert result.breakdown.known_subtotal_krw == 0
    assert result.breakdown.total_krw is None
    assert result.breakdown.status is TripCostStatus.UNKNOWN


def test_day_trip_does_not_require_accommodation() -> None:
    request = make_request(start_date="2026-10-01", end_date="2026-10-01")
    response = TripCandidateService().assemble(request, now=NOW)
    candidate = response.candidates[0]

    assert response.complete is True
    assert candidate.trip_type is TripType.DAY_TRIP
    assert candidate.nights == 0
    assert candidate.costs.required_components == ["driving"]
    assert candidate.costs.total_krw == 44_000
    assert candidate.component_statuses["accommodation"] is SourceDataStatus.NOT_REQUIRED


def test_candidate_links_each_valid_offer_and_keeps_places_deduplicated() -> None:
    hotel = make_place(
        "hotel-canonical", "Hotel A", "accommodation", source="booking", source_id="hotel-1"
    )
    restaurant = make_place("restaurant-1", "Restaurant", "restaurant", lat=35.18, lng=129.08)
    attraction = make_place("attraction-1", "Attraction", "attraction", lat=35.20, lng=129.10)
    offer_a = make_offer(source_id="hotel-1", source_offer_id="offer-a")
    offer_b = make_offer(source_id="hotel-1", source_offer_id="offer-b")
    request = make_request(
        canonical_places=[hotel, restaurant, restaurant, attraction],
        offers=[offer_b, offer_a],
    )

    response = TripCandidateService().assemble(request, now=NOW)

    assert len(response.candidates) == 2
    assert [candidate.accommodation_offer.source_offer_id for candidate in response.candidates] == [
        "offer-a",
        "offer-b",
    ]
    assert all(candidate.accommodation.id == "hotel-canonical" for candidate in response.candidates)
    assert all([place.id for place in candidate.restaurants] == ["restaurant-1"] for candidate in response.candidates)
    assert all(candidate.costs.total_krw == 284_000 for candidate in response.candidates)


def test_candidate_id_and_order_are_independent_of_input_offer_order() -> None:
    hotel = make_place(
        "hotel-canonical", "Hotel A", "accommodation", source="booking", source_id="hotel-1"
    )
    offer_a = make_offer(source_id="hotel-1", source_offer_id="offer-a")
    offer_b = make_offer(source_id="hotel-1", source_offer_id="offer-b")
    service = TripCandidateService()

    first = service.assemble(
        make_request(canonical_places=[hotel], offers=[offer_a, offer_b]),
        now=NOW,
    )
    second = service.assemble(
        make_request(canonical_places=[hotel], offers=[offer_b, offer_a]),
        now=NOW,
    )

    assert [candidate.id for candidate in first.candidates] == [candidate.id for candidate in second.candidates]
    assert first.request_fingerprint == second.request_fingerprint


def test_candidate_id_ignores_operational_warnings_and_limit() -> None:
    hotel = make_place(
        "hotel-canonical", "Hotel A", "accommodation", source="booking", source_id="hotel-1"
    )
    offer = make_offer(source_id="hotel-1", source_offer_id="offer-a")
    service = TripCandidateService()

    first = service.assemble(
        make_request(canonical_places=[hotel], offers=[offer], max_candidates=50),
        now=NOW,
    )
    second = service.assemble(
        make_request(
            canonical_places=[hotel],
            offers=[offer],
            max_candidates=1,
            source_statuses={"accommodation": SourceDataStatus.PARTIAL},
            source_warnings=["ACCOMMODATION_SOURCE_TIMEOUT"],
        ),
        now=NOW,
    )

    assert first.candidates[0].id == second.candidates[0].id


def test_repeated_snapshot_of_one_offer_does_not_create_duplicate_candidates() -> None:
    hotel = make_place(
        "hotel-canonical", "Hotel A", "accommodation", source="booking", source_id="hotel-1"
    )
    offer = make_offer(source_id="hotel-1", source_offer_id="offer-a")

    response = TripCandidateService().assemble(
        make_request(canonical_places=[hotel], offers=[offer, offer.model_copy()]),
        now=NOW,
    )

    assert len(response.candidates) == 1


def test_same_offer_id_with_different_stay_context_is_not_deduplicated() -> None:
    hotel = make_place(
        "hotel-canonical", "Hotel A", "accommodation", source="booking", source_id="hotel-1"
    )
    valid = make_offer(source_id="hotel-1", source_offer_id="same-offer")
    wrong_date = make_offer(
        source_id="hotel-1",
        source_offer_id="same-offer",
        checkin="2026-10-02",
    )

    response = TripCandidateService().assemble(
        make_request(canonical_places=[hotel], offers=[wrong_date, valid]),
        now=NOW,
    )

    assert len(response.candidates) == 1
    assert response.candidates[0].accommodation_offer.checkin.isoformat() == "2026-10-01"


def test_offer_context_mismatch_falls_back_to_a_partial_candidate() -> None:
    hotel = make_place(
        "hotel-canonical", "Hotel A", "accommodation", source="booking", source_id="hotel-1"
    )
    wrong_date = make_offer(source_id="hotel-1", checkin="2026-10-02")
    request = make_request(canonical_places=[hotel], offers=[wrong_date])

    response = TripCandidateService().assemble(request, now=NOW)

    assert len(response.candidates) == 1
    assert response.candidates[0].accommodation is None
    assert response.candidates[0].costs.total_krw is None
    assert response.candidates[0].costs.known_subtotal_krw == 44_000
    assert "ACCOMMODATION_OFFER_CONTEXT_MISMATCH" in response.warnings


def test_nearby_places_use_haversine_and_exclude_null_ratings_from_means() -> None:
    hotel = make_place(
        "hotel-canonical",
        "Hotel A",
        "accommodation",
        source="booking",
        source_id="hotel-1",
        lat=None,
        lng=None,
    )
    nearby = make_place("restaurant-near", "Near", "restaurant", lat=35.18, lng=129.08, rating=0.8)
    no_rating = make_place(
        "restaurant-no-rating",
        "No Rating",
        "restaurant",
        lat=35.181,
        lng=129.081,
        rating=None,
        rating_confidence=None,
        review_count=None,
    )
    far = make_place("restaurant-far", "Far", "restaurant", lat=35.30, lng=129.30)
    request = make_request(
        canonical_places=[hotel, nearby, no_rating, far],
        offers=[make_offer(source_id="hotel-1")],
    )

    candidate = TripCandidateService().assemble(request, now=NOW).candidates[0]

    assert {place.id for place in candidate.restaurants} == {"restaurant-near", "restaurant-no-rating"}
    assert candidate.quality.nearby_restaurant_count == 2
    assert candidate.quality.restaurant_rating_mean == pytest.approx(0.8)
    assert "ACCOMMODATION_COORDINATES_UNKNOWN" in candidate.warnings


def test_route_identity_mismatch_is_rejected() -> None:
    request = make_request()
    with pytest.raises(TripAssemblyError, match="route"):
        TripCandidateService().assemble(
            request,
            driving_cost=make_driving_cost(route_id="a-different-route"),
            now=NOW,
        )


def test_stale_route_id_is_rejected_even_when_driving_cost_matches_it() -> None:
    request = make_request()
    stale_route = request.route.model_copy(update={"route_id": "stale-route-id"})
    with pytest.raises(TripAssemblyError, match="origin and destination"):
        TripCandidateService().assemble(
            request,
            route=stale_route,
            driving_cost=make_driving_cost(route_id="stale-route-id"),
            now=NOW,
        )


def test_driving_cost_without_a_route_is_rejected() -> None:
    request = make_request(route=None, driving_cost=None)
    with pytest.raises(TripAssemblyError, match="matching route"):
        TripCandidateService().assemble(
            request,
            driving_cost=make_driving_cost(),
            now=NOW,
        )
