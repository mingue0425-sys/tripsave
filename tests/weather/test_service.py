import asyncio
from datetime import date, datetime, timedelta, timezone

from backend.weather.models import DailyWeather, WeatherForecastRequest, WeatherStatus
from backend.weather.provider import WeatherProviderError
from backend.weather.repository import WeatherCache
from backend.weather.service import WeatherService, _cache_key


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


class FailingApiProvider(FakeProvider):
    name = "kma_public"

    async def daily_forecast(self, lat, lng, start_date, end_date):
        del lat, lng, start_date, end_date
        self.calls += 1
        raise WeatherProviderError("WEATHER_SOURCE_TIMEOUT")


class WebFallbackProvider(FakeProvider):
    name = "kma_web"
    max_forecast_days = 10

    async def daily_forecast(self, lat, lng, start_date, end_date):
        result = await super().daily_forecast(lat, lng, start_date, end_date)
        return [day.model_copy(update={"source": self.name}) for day in result]


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


def test_response_and_cache_keep_provider_fetched_at(tmp_path):
    provider = FakeProvider()
    service = WeatherService(
        provider,
        WeatherCache(tmp_path / "weather.sqlite3"),
        now=lambda: datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
    )

    response = asyncio.run(
        service.forecast(_request(start=date(2026, 9, 16), end=date(2026, 9, 16)))
    )

    provider_time = datetime(2026, 9, 16, tzinfo=timezone.utc)
    assert response.fetched_at == provider_time
    assert response.forecast[0].fetched_at == provider_time
    cached = service.cache.get(
        _cache_key(_request(start=date(2026, 9, 16), end=date(2026, 9, 16)), provider.name),
        now=datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
    )
    assert cached is not None
    assert cached.fetched_at == provider_time


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
        WeatherCache(tmp_path / "weather.sqlite3", ttl_s=1, stale_max_age_s=7_200),
        now=lambda: current[0],
    )
    asyncio.run(service.forecast(_request(start=date(2026, 9, 16), end=date(2026, 9, 16))))
    provider.fail = True
    current[0] += timedelta(seconds=2)
    response = asyncio.run(service.forecast(_request(start=date(2026, 9, 16), end=date(2026, 9, 16))))
    assert response.status is WeatherStatus.STALE
    assert response.stale is True
    assert response.forecast[0].stale is True


def test_api_first_can_use_explicit_web_fallback(tmp_path):
    api = FailingApiProvider()
    web = WebFallbackProvider()
    service = WeatherService(
        api,
        WeatherCache(tmp_path / "weather.sqlite3"),
        fallback_provider=web,
        now=lambda: datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
    )

    response = asyncio.run(service.forecast(_request()))

    assert response.status is WeatherStatus.OK
    assert response.source == "kma_web"
    assert response.warnings == ["WEATHER_SOURCE_TIMEOUT"]
    assert api.calls == 1
    assert web.calls == 1


def test_timezone_boundary_is_evaluated_in_requested_local_timezone(tmp_path):
    provider = FakeProvider()
    service = WeatherService(
        provider,
        WeatherCache(tmp_path / "weather.sqlite3"),
        now=lambda: datetime(2026, 9, 15, 23, 30, tzinfo=timezone.utc),
    )
    response = asyncio.run(
        service.forecast(_request(start=date(2026, 9, 20), end=date(2026, 9, 20)))
    )

    # 23:30 UTC is 08:30 on September 16 in Asia/Seoul, so a three-day
    # provider can serve through September 19 but must not extrapolate.
    assert response.status is WeatherStatus.NOT_AVAILABLE_YET
    assert response.available_until == date(2026, 9, 19)
    assert provider.calls == 0


def test_debug_response_keeps_provider_diagnostics_separate(tmp_path):
    service = WeatherService(
        FakeProvider(),
        WeatherCache(tmp_path / "weather.sqlite3"),
        debug=True,
        now=lambda: datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
    )
    response = asyncio.run(service.forecast(_request(start=date(2026, 9, 16), end=date(2026, 9, 16))))

    assert response.diagnostics["provider_selected"] == "fixture_weather"
    assert response.diagnostics["cache_state"] == "live"


def test_provider_duplicate_date_is_fail_safe(tmp_path):
    class DuplicateProvider(FakeProvider):
        async def daily_forecast(self, lat, lng, start_date, end_date):
            result = await super().daily_forecast(lat, lng, start_date, end_date)
            return result + [result[0]]

    response = asyncio.run(
        WeatherService(
            DuplicateProvider(),
            WeatherCache(tmp_path / "weather.sqlite3"),
            now=lambda: datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
        ).forecast(_request(start=date(2026, 9, 16), end=date(2026, 9, 16)))
    )
    assert response.status is WeatherStatus.UNAVAILABLE
    assert "WEATHER_PROVIDER_DUPLICATE_DATE" in response.warnings


def test_provider_missing_date_is_not_synthesized_as_a_partial_forecast(tmp_path):
    class SparseProvider(FakeProvider):
        async def daily_forecast(self, lat, lng, start_date, end_date):
            del lat, lng, start_date, end_date
            self.calls += 1
            return []

    response = asyncio.run(
        WeatherService(
            SparseProvider(),
            WeatherCache(tmp_path / "weather.sqlite3"),
            now=lambda: datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
        ).forecast(_request(start=date(2026, 9, 16), end=date(2026, 9, 16)))
    )

    assert response.status is WeatherStatus.PARTIAL
    assert response.forecast[0].status is WeatherStatus.NOT_AVAILABLE_YET
    assert response.forecast[0].temp_min_c is None


def test_web_provider_horizon_error_is_not_reported_as_source_unavailable(tmp_path):
    class HorizonProvider(FakeProvider):
        name = "kma_web"
        max_forecast_days = 10

        async def daily_forecast(self, lat, lng, start_date, end_date):
            del lat, lng, start_date, end_date
            self.calls += 1
            raise WeatherProviderError("KMA_WEB_FORECAST_NOT_AVAILABLE")

    response = asyncio.run(
        WeatherService(
            HorizonProvider(),
            WeatherCache(tmp_path / "weather.sqlite3"),
            now=lambda: datetime(2026, 9, 16, 1, tzinfo=timezone.utc),
        ).forecast(_request(start=date(2026, 9, 20), end=date(2026, 9, 20)))
    )

    assert response.status is WeatherStatus.NOT_AVAILABLE_YET
    assert "KMA_WEB_FORECAST_NOT_AVAILABLE" in response.warnings
