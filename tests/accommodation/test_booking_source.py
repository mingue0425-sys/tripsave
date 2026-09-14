from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.models import Location
from crawler.accommodation.booking import BookingComSource, build_booking_search_url
from crawler.accommodation.errors import (
    AccommodationAccessDeniedError,
    AccommodationSourceError,
)


def test_booking_search_url_keeps_date_and_occupancy_contract() -> None:
    url = build_booking_search_url(
        Location(lat=35.1796, lng=129.0756, label="Busan"),
        date(2026, 10, 1),
        date(2026, 10, 2),
        2,
        1,
    )
    query = parse_qs(urlsplit(url).query)

    assert urlsplit(url).hostname == "www.booking.com"
    assert query["ss"] == ["Busan"]
    assert query["checkin"] == ["2026-10-01"]
    assert query["checkout"] == ["2026-10-02"]
    assert query["group_adults"] == ["2"]
    assert query["group_children"] == ["1"]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_booking_access_denial_is_explicit(status: int) -> None:
    with pytest.raises(AccommodationAccessDeniedError):
        BookingComSource._raise_for_response_status(status)


def test_booking_http_error_is_not_treated_as_empty_results() -> None:
    with pytest.raises(AccommodationSourceError):
        BookingComSource._raise_for_response_status(500)
