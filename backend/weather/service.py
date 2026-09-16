"""Weather orchestration with horizon, cache, and stale fallback semantics."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import (
    DailyWeather,
    WeatherForecastRequest,
    WeatherForecastResponse,
    WeatherStatus,
)
from .provider import WeatherProvider, WeatherProviderError
from .repository import WeatherCache


def _cache_key(request: WeatherForecastRequest, provider_name: str) -> str:
    payload = {
        "provider": provider_name,
        "lat": round(request.lat, 2),
        "lng": round(request.lng, 2),
        "start": request.start_date.isoformat(),
        "end": request.end_date.isoformat(),
        "timezone": request.timezone,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _unknown_days(
    start_date: date,
    end_date: date,
    *,
    source: str,
    fetched_at: datetime,
    status: WeatherStatus,
) -> list[DailyWeather]:
    result: list[DailyWeather] = []
    current = start_date
    while current <= end_date:
        result.append(
            DailyWeather(
                date=current,
                source=source,
                fetched_at=fetched_at,
                status=status,
                stale=status is WeatherStatus.STALE,
            )
        )
        current += timedelta(days=1)
    return result


def _response_status(forecast: list[DailyWeather], *, stale: bool, unavailable: bool = False) -> WeatherStatus:
    if stale:
        return WeatherStatus.STALE
    if unavailable:
        return WeatherStatus.UNAVAILABLE
    if not forecast:
        return WeatherStatus.EMPTY
    if all(day.status is WeatherStatus.OK for day in forecast):
        return WeatherStatus.OK
    return WeatherStatus.PARTIAL


class WeatherService:
    def __init__(
        self,
        provider: WeatherProvider,
        cache: WeatherCache,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self._now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception as error:
            raise ValueError("timezone is not installed on this server") from error

    @staticmethod
    def _mark_stale(days: list[DailyWeather]) -> list[DailyWeather]:
        return [day.model_copy(update={"status": WeatherStatus.STALE, "stale": True}) for day in days]

    async def forecast(self, request: WeatherForecastRequest) -> WeatherForecastResponse:
        timezone_info = self._timezone(request.timezone)
        raw_now = self._now()
        now = raw_now if raw_now.tzinfo else raw_now.replace(tzinfo=timezone.utc)
        now_local = now.astimezone(timezone_info)
        fetched_at = now
        provider_name = self.provider.name
        key = _cache_key(request, provider_name)
        cached = await asyncio.to_thread(self.cache.get, key, now=now)
        if cached is not None and cached.fresh:
            forecast = [DailyWeather.model_validate(item) for item in cached.payload]
            return WeatherForecastResponse(
                status=_response_status(forecast, stale=False),
                complete=all(day.status is WeatherStatus.OK for day in forecast),
                forecast=forecast,
                source=provider_name,
                fetched_at=cached.fetched_at,
                timezone=request.timezone,
                warnings=["WEATHER_CACHE_HIT"],
            )

        supported_end = now_local.date() + timedelta(days=self.provider.max_forecast_days)
        if request.start_date > supported_end:
            unknown = _unknown_days(
                request.start_date,
                request.end_date,
                source=provider_name,
                fetched_at=fetched_at,
                status=WeatherStatus.NOT_AVAILABLE_YET,
            )
            return WeatherForecastResponse(
                status=WeatherStatus.NOT_AVAILABLE_YET,
                complete=False,
                forecast=unknown,
                source=provider_name,
                fetched_at=fetched_at,
                timezone=request.timezone,
                warnings=["FORECAST_HORIZON_EXCEEDED"],
                available_until=supported_end,
            )

        query_end = min(request.end_date, supported_end)
        try:
            forecast = await self.provider.daily_forecast(
                request.lat,
                request.lng,
                request.start_date,
                query_end,
            )
            by_date = {day.date: day for day in forecast}
            for missing_date in (
                request.start_date + timedelta(days=offset)
                for offset in range((query_end - request.start_date).days + 1)
            ):
                by_date.setdefault(
                    missing_date,
                    DailyWeather(
                        date=missing_date,
                        source=provider_name,
                        fetched_at=fetched_at,
                        status=WeatherStatus.PARTIAL,
                    ),
                )
            forecast = [by_date[current] for current in sorted(by_date)]
            if query_end < request.end_date:
                forecast.extend(
                    _unknown_days(
                        query_end + timedelta(days=1),
                        request.end_date,
                        source=provider_name,
                        fetched_at=fetched_at,
                        status=WeatherStatus.NOT_AVAILABLE_YET,
                    )
                )
            payload = [day.model_dump(mode="json") for day in forecast]
            await asyncio.to_thread(
                self.cache.put,
                key,
                provider=provider_name,
                payload=payload,
                fetched_at=fetched_at,
            )
            return WeatherForecastResponse(
                status=_response_status(forecast, stale=False),
                complete=all(day.status is WeatherStatus.OK for day in forecast),
                forecast=forecast,
                source=provider_name,
                fetched_at=fetched_at,
                timezone=request.timezone,
                warnings=([] if query_end == request.end_date else ["FORECAST_HORIZON_EXCEEDED"]),
                available_until=supported_end if query_end < request.end_date else None,
            )
        except WeatherProviderError as error:
            if cached is not None:
                stale_forecast = self._mark_stale(
                    [DailyWeather.model_validate(item) for item in cached.payload]
                )
                return WeatherForecastResponse(
                    status=WeatherStatus.STALE,
                    complete=False,
                    forecast=stale_forecast,
                    source=provider_name,
                    fetched_at=cached.fetched_at,
                    stale=True,
                    timezone=request.timezone,
                    warnings=[error.code, "WEATHER_STALE_CACHE"],
                )
            unknown = _unknown_days(
                request.start_date,
                request.end_date,
                source=provider_name,
                fetched_at=fetched_at,
                status=WeatherStatus.UNAVAILABLE,
            )
            return WeatherForecastResponse(
                status=WeatherStatus.UNAVAILABLE,
                complete=False,
                forecast=unknown,
                source=provider_name,
                fetched_at=fetched_at,
                timezone=request.timezone,
                warnings=[error.code],
                available_until=supported_end,
            )

    async def status(self) -> dict[str, object]:
        return {
            "provider": self.provider.name,
            "max_forecast_days": self.provider.max_forecast_days,
            "configured": getattr(self.provider, "api_key", None) is not None,
        }
