from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.places.models import (
    PlaceCategory,
    PlaceRecord,
    PlaceSearchRequest,
)
from backend.accommodation.models import PlaceRecord as AccommodationPlaceRecord
from backend.place_models import PlaceSourceRecord


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


def test_v06_and_v07_place_records_share_only_source_preserving_contract() -> None:
    common_fields = {
        "source",
        "source_id",
        "source_url",
        "name",
        "category",
        "lat",
        "lng",
        "address",
        "rating",
        "rating_scale",
        "review_count",
        "fetched_at",
    }
    assert common_fields <= set(PlaceSourceRecord.model_fields)
    assert common_fields <= set(PlaceRecord.model_fields)
    assert common_fields <= set(AccommodationPlaceRecord.model_fields)
    assert issubclass(PlaceRecord, PlaceSourceRecord)
    assert issubclass(AccommodationPlaceRecord, PlaceSourceRecord)


@pytest.mark.parametrize("record_type, category", [(PlaceRecord, "restaurant"), (AccommodationPlaceRecord, "accommodation")])
def test_source_records_keep_unknown_numeric_values_distinct_from_zero(
    record_type, category
) -> None:
    common = {
        "source": "example",
        "name": "A Place",
        "category": category,
        "fetched_at": datetime.now(timezone.utc),
    }
    unknown = record_type(**common, rating=None, rating_scale=None, review_count=None)
    zero = record_type(**common, rating=0.0, rating_scale=5.0, review_count=0)
    assert unknown.rating is None and unknown.review_count is None
    assert zero.rating == 0.0 and zero.review_count == 0
    with pytest.raises(ValidationError):
        record_type(**common, rating=4.0, rating_scale=None)
    with pytest.raises(ValidationError):
        record_type(**common, lat=float("nan"), lng=126.9)
