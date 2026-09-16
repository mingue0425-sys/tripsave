import sqlite3
from datetime import datetime, timedelta, timezone

from backend.weather.repository import WeatherCache


def test_weather_cache_preserves_provider_metadata_and_stale_window(tmp_path):
    cache = WeatherCache(tmp_path / "weather.sqlite3", ttl_s=10, stale_max_age_s=30)
    fetched_at = datetime(2026, 9, 16, 1, tzinfo=timezone.utc)
    payload = [
        {
            "date": "2026-09-16",
            "condition": None,
            "source": "kma_web",
            "source_url": "https://www.weather.go.kr/w/forecast/overall/short-term.do",
            "fetched_at": fetched_at.isoformat(),
            "status": "partial",
        }
    ]
    cache.put("web-key", provider="kma_web", payload=payload, fetched_at=fetched_at)

    fresh = cache.get("web-key", now=fetched_at + timedelta(seconds=5))
    stale = cache.get("web-key", now=fetched_at + timedelta(seconds=15))
    expired = cache.get("web-key", now=fetched_at + timedelta(seconds=41))

    assert fresh is not None and fresh.fresh is True and fresh.provider == "kma_web"
    assert stale is not None and stale.fresh is False
    assert expired is None


def test_corrupt_cache_payload_is_a_miss(tmp_path):
    path = tmp_path / "weather.sqlite3"
    cache = WeatherCache(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE weather_cache SET payload_json = ? WHERE cache_key = ?",
            ("{not-json", "missing"),
        )
        connection.execute(
            """
            INSERT INTO weather_cache(cache_key, provider, payload_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "corrupt",
                "kma_web",
                "{not-json",
                "2026-09-16T01:00:00+00:00",
                "2026-09-16T04:00:00+00:00",
            ),
        )

    assert cache.get("corrupt", now=datetime(2026, 9, 16, 2, tzinfo=timezone.utc)) is None
