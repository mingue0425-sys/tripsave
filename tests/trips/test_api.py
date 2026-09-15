import asyncio

from fastapi.testclient import TestClient

import app as app_module
from backend.accommodation.models import (
    AccommodationResult,
    AccommodationSearchResponse,
)
from backend.accommodation.models import (
    PlaceRecord as AccommodationPlaceRecord,
)
from backend.places.models import PlaceCategory, PlaceRecord, PlaceSearchResponse
from backend.trips.models import SourceDataStatus
from crawler.accommodation.errors import AccommodationSourceTimeoutError
from tests.trips.helpers import make_offer, make_place, make_request


def test_trip_candidates_api_assembles_supplied_dependencies_without_fetching(
    monkeypatch,
) -> None:
    hotel = make_place(
        "hotel-canonical", "Hotel A", "accommodation", source="booking", source_id="hotel-1"
    )
    restaurant = make_place("restaurant-1", "Restaurant", "restaurant", lat=35.18, lng=129.08)
    attraction = make_place("attraction-1", "Attraction", "attraction", lat=35.20, lng=129.10)
    request = make_request(canonical_places=[hotel, restaurant, attraction], offers=[make_offer()])

    async def fake_loader(value):
        return (
            value.route,
            value.driving_cost,
            list(value.canonical_places or []),
            list(value.offers or []),
            {
                "driving": SourceDataStatus.OK,
                "accommodation": SourceDataStatus.OK,
                "restaurants": SourceDataStatus.OK,
                "attractions": SourceDataStatus.OK,
            },
            [],
        )

    monkeypatch.setattr(app_module, "_load_trip_candidate_dependencies", fake_loader)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/trips/candidates",
            json=request.model_dump(mode="json", exclude_none=True),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["complete"] is True
    assert len(body["candidates"]) == 1
    candidate = body["candidates"][0]
    assert candidate["costs"]["driving_krw"] == 44_000
    assert candidate["costs"]["accommodation_krw"] == 240_000
    assert candidate["costs"]["total_krw"] == 284_000
    assert candidate["quality"]["nearby_restaurant_count"] == 1
    assert candidate["quality"]["nearby_attraction_count"] == 1


def test_trip_candidates_api_returns_partial_without_erasing_places_when_booking_is_unavailable(
    monkeypatch,
) -> None:
    restaurant = make_place("restaurant-1", "Restaurant", "restaurant", lat=35.18, lng=129.08)
    request = make_request(
        canonical_places=[restaurant],
        offers=[],
        source_statuses={
            "driving": SourceDataStatus.OK,
            "accommodation": SourceDataStatus.UNAVAILABLE,
            "restaurants": SourceDataStatus.OK,
            "attractions": SourceDataStatus.EMPTY,
        },
        source_warnings=["ACCOMMODATION_SOURCE_TIMEOUT"],
    )

    async def fake_loader(value):
        return (
            value.route,
            value.driving_cost,
            list(value.canonical_places or []),
            [],
            dict(value.source_statuses),
            list(value.source_warnings),
        )

    monkeypatch.setattr(app_module, "_load_trip_candidate_dependencies", fake_loader)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/trips/candidates",
            json=request.model_dump(mode="json", exclude_none=True),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["complete"] is False
    candidate = body["candidates"][0]
    assert candidate["costs"]["known_subtotal_krw"] == 44_000
    assert candidate["costs"]["total_krw"] is None
    assert candidate["component_statuses"]["accommodation"] == "unavailable"
    assert [place["id"] for place in candidate["restaurants"]] == ["restaurant-1"]
    assert "ACCOMMODATION_SOURCE_TIMEOUT" in body["warnings"]


def test_dependency_loader_isolates_accommodation_failure_from_places(monkeypatch) -> None:
    request = make_request(canonical_places=None, offers=None)
    place = PlaceRecord(
        source="visitkorea",
        source_id="restaurant-live-1",
        source_url="https://english.visitkorea.or.kr/detail",
        name="Restaurant",
        category=PlaceCategory.RESTAURANT,
        lat=35.18,
        lng=129.08,
        address="Busan address",
        rating=4.5,
        rating_scale=5.0,
        review_count=25,
        fetched_at="2026-09-15T00:00:00Z",
    )

    class FailingAccommodationService:
        async def search(self, _request):
            raise AccommodationSourceTimeoutError()

    class WorkingPlacesService:
        async def search(self, _request):
            return PlaceSearchResponse(
                complete=True,
                results=[place],
                issues=[],
                cache_hit=False,
                source_status="fresh",
                fetched_at="2026-09-15T00:00:00Z",
            )

    monkeypatch.setattr(app_module, "accommodation_service", FailingAccommodationService())
    monkeypatch.setattr(app_module, "places_service", WorkingPlacesService())

    route, driving_cost, canonical_places, offers, statuses, warnings = asyncio.run(
        app_module._load_trip_candidate_dependencies(request)
    )

    assert route is not None
    assert driving_cost is not None
    assert offers == []
    assert [value.category for value in canonical_places] == [PlaceCategory.RESTAURANT]
    assert statuses["accommodation"] is SourceDataStatus.UNAVAILABLE
    assert statuses["restaurants"] is SourceDataStatus.OK
    assert statuses["attractions"] is SourceDataStatus.EMPTY
    assert "ACCOMMODATION_SOURCE_TIMEOUT" in warnings


def test_dependency_loader_keeps_accommodation_when_places_source_fails(monkeypatch) -> None:
    request = make_request(canonical_places=None, offers=None)
    offer = make_offer()
    hotel = AccommodationPlaceRecord(
        source="booking",
        source_id="hotel-1",
        source_url="https://www.booking.com/hotel/kr/sample.html",
        name="Hotel A",
        category="accommodation",
        address="Busan address",
        fetched_at="2026-09-15T00:00:00Z",
    )

    class WorkingAccommodationService:
        async def search(self, _request):
            return AccommodationSearchResponse(
                complete=True,
                results=[AccommodationResult(place=hotel, offers=[offer])],
                source_status="fresh",
                cache_hit=False,
                fetched_at="2026-09-15T00:00:00Z",
                issues=[],
            )

    class FailingPlacesService:
        async def search(self, _request):
            raise RuntimeError("source unavailable")

    monkeypatch.setattr(app_module, "accommodation_service", WorkingAccommodationService())
    monkeypatch.setattr(app_module, "places_service", FailingPlacesService())

    _route, _driving_cost, canonical_places, offers, statuses, warnings = asyncio.run(
        app_module._load_trip_candidate_dependencies(request)
    )

    assert [value.category for value in canonical_places] == ["accommodation"]
    assert len(offers) == 1
    assert statuses["accommodation"] is SourceDataStatus.OK
    assert statuses["restaurants"] is SourceDataStatus.UNAVAILABLE
    assert statuses["attractions"] is SourceDataStatus.UNAVAILABLE
    assert "PLACES_SOURCE_UNAVAILABLE" in warnings


def test_trip_candidates_api_rejects_checkout_before_checkin() -> None:
    request = make_request()
    payload = request.model_dump(mode="json", exclude_none=True)
    payload["start_date"] = "2026-10-03"
    payload["end_date"] = "2026-10-01"
    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/trips/candidates",
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
