from datetime import date, datetime, timezone

from backend.accommodation.models import AccommodationOffer, AccommodationResult, PlaceRecord


def make_result(
    *,
    source: str = "test_source",
    source_id: str = "kr/test-hotel",
    final_price_krw: int | None = 125_000,
    availability: bool | None = True,
) -> AccommodationResult:
    fetched_at = datetime(2026, 9, 12, tzinfo=timezone.utc)
    place = PlaceRecord(
        source=source,
        source_id=source_id,
        source_url=f"https://example.test/{source_id}",
        name="Test Hotel",
        category="accommodation",
        lat=None,
        lng=None,
        address="Seoul",
        rating=8.5,
        rating_scale=10,
        review_count=100,
        fetched_at=fetched_at,
    )
    offer = AccommodationOffer(
        source=source,
        source_offer_id="offer-1",
        place_source_id=source_id,
        checkin=date(2026, 10, 1),
        checkout=date(2026, 10, 2),
        adults=2,
        children=0,
        room_name="Standard Room",
        base_price_krw=None,
        taxes_krw=None,
        final_price_krw=final_price_krw,
        availability=availability,
        fetched_at=fetched_at,
    )
    return AccommodationResult(
        place=place,
        offers=[offer],
        distance_km=1.2,
        distance_text="1.2 km from downtown",
    )
