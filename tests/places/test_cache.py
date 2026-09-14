from datetime import datetime, timedelta, timezone

from crawler.places.cache import PlaceCache, make_cache_key
from tests.places.fixtures import FETCHED_AT, record_payload
from backend.places.models import PlaceRecord


def test_place_cache_round_trips_records_and_key_is_stable(tmp_path) -> None:
    cache = PlaceCache(tmp_path / "places.sqlite3")
    key_a = make_cache_key(
        source="visitkorea",
        category="restaurant",
        label="Busan",
        lat=35.1796,
        lng=129.0756,
        radius_km=10,
    )
    key_b = make_cache_key(
        source="visitkorea",
        category="restaurant",
        label=" busan ",
        lat=35.1796,
        lng=129.0756,
        radius_km=10.0,
    )
    assert key_a == key_b
    cache.put(
        key_a,
        source="visitkorea",
        category="restaurant",
        results=[PlaceRecord.model_validate(record_payload())],
        fetched_at=FETCHED_AT,
    )

    entry = cache.get(key_a)
    assert entry is not None
    assert entry.results[0].name == "Fixture Kitchen"
    assert cache.get(key_a, max_age_seconds=60) is None


def test_place_cache_freshness_can_be_checked_with_a_recent_entry(tmp_path) -> None:
    cache = PlaceCache(tmp_path / "places.sqlite3")
    key = "fresh-key"
    now = datetime.now(timezone.utc)
    cache.put(
        key,
        source="visitkorea",
        category="attraction",
        results=[PlaceRecord.model_validate(record_payload(category="attraction"))],
        fetched_at=now,
    )

    assert cache.get(key, max_age_seconds=60) is not None
    assert cache.get(key, max_age_seconds=0.0) is None
