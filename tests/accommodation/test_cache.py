import sqlite3
from datetime import datetime, timedelta, timezone

from backend.accommodation.models import (
    AccommodationPriceBasis,
    AccommodationSearchRequest,
)
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
    result = result.model_copy(
        update={
            "offers": [
                result.offers[0].model_copy(
                    update={
                        "price_basis": AccommodationPriceBasis.TOTAL_STAY,
                        "price_freshness": "fresh",
                    }
                )
            ]
        }
    )

    cache.put("test_source", request, [result], complete=True)
    cached = cache.get("test_source", request)

    assert cached is not None
    assert cached.fresh is True
    assert cached.complete is True
    assert cached.results[0].place.name == "Test Hotel"
    assert cached.results[0].offers[0].final_price_krw == 125_000
    assert cached.results[0].offers[0].price_basis is AccommodationPriceBasis.TOTAL_STAY
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


def test_cache_adds_v09_offer_columns_without_dropping_legacy_rows(tmp_path) -> None:
    database = tmp_path / "legacy-accommodation.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE accommodation_offers (
            offer_cache_key TEXT PRIMARY KEY,
            search_cache_key TEXT,
            result_index INTEGER,
            source TEXT,
            source_offer_id TEXT,
            place_key TEXT,
            place_source_id TEXT,
            checkin TEXT,
            checkout TEXT,
            adults INTEGER,
            children INTEGER,
            room_name TEXT,
            base_price_krw INTEGER,
            taxes_krw INTEGER,
            final_price_krw INTEGER,
            availability INTEGER,
            distance_km REAL,
            distance_text TEXT,
            fetched_at TEXT,
            expires_at TEXT,
            parser_version TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO accommodation_offers (offer_cache_key, final_price_krw) VALUES (?, ?)",
        ("legacy", 100_000),
    )
    connection.commit()
    connection.close()

    cache = AccommodationCache(database)
    connection = cache._connection()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(accommodation_offers)")}
    price = connection.execute(
        "SELECT final_price_krw FROM accommodation_offers WHERE offer_cache_key = ?",
        ("legacy",),
    ).fetchone()[0]
    connection.close()

    assert {"price_basis", "price_freshness"} <= columns
    assert price == 100_000


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
