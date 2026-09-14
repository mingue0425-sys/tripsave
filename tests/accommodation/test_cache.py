import sqlite3
from datetime import datetime, timedelta, timezone

from backend.accommodation.models import AccommodationSearchRequest
from backend.models import Location
from crawler.accommodation.cache import AccommodationCache, make_search_cache_key

from .helpers import make_result


def make_request() -> AccommodationSearchRequest:
    return AccommodationSearchRequest(
        destination=Location(lat=37.5665, lng=126.978, label="Seoul"),
        checkin="2026-10-01",
        checkout="2026-10-02",
        adults=2,
        children=0,
    )


def test_cache_round_trips_place_and_offer_with_separate_tables(tmp_path) -> None:
    cache = AccommodationCache(tmp_path / "accommodation.db", place_ttl_s=86_400, offer_ttl_s=3_600)
    request = make_request()
    result = make_result()

    cache.put("test_source", request, [result], complete=True)
    cached = cache.get("test_source", request)

    assert cached is not None
    assert cached.fresh is True
    assert cached.complete is True
    assert cached.results[0].place.name == "Test Hotel"
    assert cached.results[0].offers[0].final_price_krw == 125_000
    connection = sqlite3.connect(tmp_path / "accommodation.db")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    connection.close()
    assert "accommodation_source_places" in tables
    assert "accommodation_offers" in tables


def test_cache_does_not_serve_expired_offer_by_default(tmp_path) -> None:
    database = tmp_path / "accommodation.db"
    cache = AccommodationCache(database, place_ttl_s=86_400, offer_ttl_s=3_600)
    request = make_request()
    cache.put("test_source", request, [make_result()], complete=True)
    connection = sqlite3.connect(database)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    connection.execute("UPDATE accommodation_offers SET expires_at = ?", (expired,))
    connection.execute("UPDATE accommodation_search_cache SET expires_at = ?", (expired,))
    connection.commit()
    connection.close()

    assert cache.get("test_source", request) is None
    stale = cache.get("test_source", request, allow_stale=True)
    assert stale is not None
    assert stale.fresh is False


def test_empty_result_is_cacheable_without_inventing_a_price(tmp_path) -> None:
    cache = AccommodationCache(tmp_path / "accommodation.db", offer_ttl_s=3_600)
    request = make_request()

    cache.put("test_source", request, [], complete=True)
    cached = cache.get("test_source", request)

    assert cached is not None
    assert cached.results == []
    assert cached.complete is True


def test_cache_key_changes_with_dates_and_occupancy(tmp_path) -> None:
    first = make_request()
    second = first.model_copy(update={"adults": 3})

    assert make_search_cache_key("test_source", first) != make_search_cache_key("test_source", second)
