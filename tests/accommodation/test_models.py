from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from backend.accommodation.models import (
    AccommodationOffer,
    AccommodationSearchRequest,
    PlaceRecord,
)
from backend.models import Location


def test_place_record_preserves_source_rating_scale_and_allows_unknown_coordinates() -> None:
    place = PlaceRecord(
        source="booking",
        source_id="kr/sample",
        source_url="https://www.booking.com/hotel/kr/sample.html",
        name="Sample",
        category="accommodation",
        lat=None,
        lng=None,
        address="Seoul",
        rating=8.9,
        rating_scale=10,
        review_count=321,
        fetched_at=datetime.now(timezone.utc),
    )

    assert place.category == "accommodation"
    assert place.rating_scale == 10
    assert place.lat is None and place.lng is None


def test_place_record_rejects_half_coordinate() -> None:
    with pytest.raises(ValidationError):
        PlaceRecord(
            source="booking",
            name="Invalid",
            category="accommodation",
            lat=37.5,
            lng=None,
            fetched_at=datetime.now(timezone.utc),
        )


def test_offer_requires_ordered_dates_and_never_accepts_zero_price() -> None:
    common = {
        "source": "booking",
        "place_source_id": "kr/sample",
        "checkin": date(2026, 10, 1),
        "checkout": date(2026, 10, 2),
        "adults": 2,
        "children": 0,
        "fetched_at": datetime.now(timezone.utc),
    }
    with pytest.raises(ValidationError):
        AccommodationOffer(**common, final_price_krw=0)
    with pytest.raises(ValidationError):
        AccommodationOffer(
            **{**common, "checkout": date(2026, 10, 1)},
            final_price_krw=100_000,
        )


def test_search_request_validates_date_order_and_occupancy() -> None:
    request = AccommodationSearchRequest(
        destination=Location(lat=35.1796, lng=129.0756, label="Busan"),
        checkin=date(2026, 10, 1),
        checkout=date(2026, 10, 2),
        adults=2,
        children=1,
    )
    assert request.destination.label == "Busan"
    with pytest.raises(ValidationError):
        AccommodationSearchRequest(
            destination=request.destination,
            checkin=date(2026, 10, 2),
            checkout=date(2026, 10, 1),
            adults=2,
        )
