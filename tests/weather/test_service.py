import asyncio
from datetime import date, datetime, timedelta, timezone

from backend.weather.models import DailyWeather, WeatherForecastRequest, WeatherStatus
from backend.weather.provider import WeatherProviderError
from backend.weather.repository import WeatherCache
from backend.weather.service import WeatherService


class FakeProvider:
    name = "fixture_weather"
    max_forecast_days = 3

    def __init__(self):
        self.calls = 0
        self.fail = False

    async def daily_forecast(self, lat, lng, start_date, end_date):
        del lat, lng
        self.calls += 1
        if self.fail:
            raise WeatherProviderError("WEATHER_SOURCE_TIMEOUT")
        fetched_at = datetime(2026, 9, 16, tzinfo=timezone.utc)
        result = []
        current = start_date
        while current <= end_date:
            result.append(
                DailyWeather(
                    date=current,
                    condition="맑음",
                    temp_min_c=14,
                    temp_max_c=23,
                    precipitation_probability_pct=10,
                    precipitation_mm=0,
                    wind_speed_mps=2.4,
                    source=self.name,
                    fetched_at=fetched_at,
                )
            )
            current += timedelta(days=1)
        return result


def _request(start=date(2026, 9, 16), end=date(2026, 9, 17)):
    return WeatherForecastRequest(
        lat=35.1796,
        lng=129.0756,
        start_date=start,
        end_date=end,
    )


def test_weather_cache_hit_and_zero_precipitation_is_real_value(tmp_path):
    provider = FakeProvider()
    service = WeatherService(
        provider,
        WeatherCache(tmp_path / "weather.sqlite3"),
        now=lambda: datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
    )
    first = asyncio.run(service.forecast(_request()))
    second = asyncio.run(service.forecast(_request()))
    assert first.status is WeatherStatus.OK
    assert second.warnings == ["WEATHER_CACHE_HIT"]
    assert provider.calls == 1
    assert second.forecast[0].precipitation_mm == 0


def test_provider_failure_is_unavailable_and_unknown_fields_stay_null(tmp_path):
    provider = FakeProvider()
    provider.fail = True
    service = WeatherService(
        provider,
        WeatherCache(tmp_path / "weather.sqlite3"),
        now=lambda: datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
    )
    response = asyncio.run(service.forecast(_request(start=date(2026, 9, 16), end=date(2026, 9, 16))))
    assert response.status is WeatherStatus.UNAVAILABLE
    assert response.forecast[0].precipitation_mm is None
    assert response.forecast[0].precipitation_probability_pct is None
    assert response.forecast[0].wind_speed_mps is None


def test_forecast_horizon_is_explicitly_unavailable(tmp_path):
    service = WeatherService(
        FakeProvider(),
        WeatherCache(tmp_path / "weather.sqlite3"),
        now=lambda: datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
    )
    response = asyncio.run(service.forecast(
        _request(start=date(2026, 9, 25), end=date(2026, 9, 25))
    ))
    assert response.status is WeatherStatus.NOT_AVAILABLE_YET
    assert response.forecast[0].condition is None
    assert response.forecast[0].precipitation_mm is None


def test_stale_cache_is_labeled(tmp_path):
    provider = FakeProvider()
    current = [datetime(2026, 9, 16, 1, tzinfo=timezone.utc)]
    service = WeatherService(
        provider,
        WeatherCache(tmp_path / "weather.sqlite3", ttl_s=1, stale_max_age_s=100),
        now=lambda: current[0],
    )
    asyncio.run(service.forecast(_request(start=date(2026, 9, 16), end=date(2026, 9, 16))))
    provider.fail = True
    current[0] += timedelta(seconds=2)
    response = asyncio.run(service.forecast(_request(start=date(2026, 9, 16), end=date(2026, 9, 16))))
    assert response.status is WeatherStatus.STALE
    assert response.stale is True
    assert response.forecast[0].stale is True
