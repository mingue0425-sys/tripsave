from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.places.models import (
    PlaceCategory,
    PlaceRecord,
    PlaceSearchRequest,
)


def test_place_record_preserves_source_rating_scale_and_optional_fields() -> None:
    record = PlaceRecord(
        source="example",
        source_id="abc",
        source_url="https://example.test/place/abc",
        name="A Place",
        category=PlaceCategory.RESTAURANT,
        lat=35.1,
        lng=129.1,
        address="1 Example-ro",
        rating=4.6,
        rating_scale=5,
        review_count=123,
        fetched_at=datetime.now(timezone.utc),
        raw_category="Food",
        tags=["korean"],
    )

    assert record.category == PlaceCategory.RESTAURANT
    assert record.rating_scale == 5
    assert record.review_count == 123
    assert record.raw_category == "Food"


def test_coordinates_must_be_supplied_together() -> None:
    with pytest.raises(ValidationError):
        PlaceRecord(
            source="example",
            name="A Place",
            category="attraction",
            lat=35.1,
            fetched_at=datetime.now(timezone.utc),
        )


def test_search_request_rejects_duplicate_categories() -> None:
    with pytest.raises(ValidationError):
        PlaceSearchRequest(
            destination={"lat": 35.1, "lng": 129.1, "label": "Busan"},
            categories=["restaurant", "restaurant"],
        )
