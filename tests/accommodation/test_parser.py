from datetime import date, datetime, timezone

import pytest

from crawler.accommodation.errors import AccommodationPageChangedError
from crawler.accommodation.parser import (
    parse_booking_search_html,
    parse_distance,
    parse_krw_amounts,
    parse_price_components,
    parse_rating_and_reviews,
)

from .fixtures import BOOKING_SEARCH_HTML


def test_price_parser_distinguishes_included_and_explicit_tax_amounts() -> None:
    included = parse_price_components("KRW 100,000", "Includes taxes and fees")
    explicit = parse_price_components("KRW 200,000", "+KRW 20,000 taxes and fees")

    assert included.base_price_krw is None
    assert included.taxes_krw is None
    assert included.final_price_krw == 100_000
    assert explicit.base_price_krw == 200_000
    assert explicit.taxes_krw == 20_000
    assert explicit.final_price_krw == 220_000

    unknown = parse_price_components("KRW 150,000", "Taxes and fees may apply")
    assert unknown.base_price_krw == 150_000
    assert unknown.taxes_krw is None
    assert unknown.final_price_krw is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("KRW 1,234", [1234]), ("₩55,000", [55000]), ("1,234 KRW", [1234])],
)
def test_currency_parser_accepts_krw_forms(value: str, expected: list[int]) -> None:
    assert parse_krw_amounts(value) == expected


def test_currency_parser_does_not_turn_other_currency_into_krw() -> None:
    assert parse_krw_amounts("US$100") == []
    assert parse_price_components("US$100", "").final_price_krw is None


def test_rating_review_and_distance_parsers_preserve_scale() -> None:
    assert parse_rating_and_reviews("Scored 8.7 Wonderful 1,234 reviews") == (8.7, 10.0, 1234)
    assert parse_distance("1.2 km from downtown") == 1.2
    assert parse_distance("Show on map") is None


def test_booking_parser_creates_separate_place_and_offer_records() -> None:
    fetched_at = datetime(2026, 9, 12, tzinfo=timezone.utc)
    results = parse_booking_search_html(
        BOOKING_SEARCH_HTML,
        source_url="https://www.booking.com/searchresults.html?ss=Seoul",
        checkin=date(2026, 10, 1),
        checkout=date(2026, 10, 2),
        adults=2,
        children=0,
        fetched_at=fetched_at,
    )

    assert len(results) == 2
    assert results[0].place.source == "booking"
    assert results[0].place.source_id == "kr/sample-hotel"
    assert results[0].place.category == "accommodation"
    assert results[0].place.rating == 8.7
    assert results[0].place.rating_scale == 10.0
    assert results[0].place.review_count == 467
    assert results[0].place.lat is None and results[0].place.lng is None
    assert results[0].offers[0].room_name == "Deluxe Room"
    assert results[0].offers[0].final_price_krw == 100_000
    assert results[0].offers[0].base_price_krw is None
    assert results[1].offers[0].taxes_krw == 20_000
    assert results[1].offers[0].final_price_krw == 220_000
    assert results[0].offers[0].checkin == date(2026, 10, 1)


def test_booking_parser_marks_changed_page_explicitly() -> None:
    with pytest.raises(AccommodationPageChangedError):
        parse_booking_search_html(
            "<html><body><p>changed</p></body></html>",
            source_url="https://www.booking.com/searchresults.html",
            checkin=date(2026, 10, 1),
            checkout=date(2026, 10, 2),
            adults=2,
            children=0,
        )
