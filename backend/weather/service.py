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
from .repository import WeatherCache, WeatherCacheEntry


def _cache_key(request: WeatherForecastRequest, provider_name: str) -> str:
    payload = {
        "provider": provider_name,
        # Five decimals are roughly metre-scale in latitude.  A two-decimal
        # key could make nearby, but different, KMA areas share a forecast.
        "lat": round(request.lat, 5),
        "lng": round(request.lng, 5),
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
                missing_fields=[
                    "condition",
                    "temp_min_c",
                    "temp_max_c",
                    "precipitation_probability_pct",
                    "precipitation",
                    "wind_speed_mps",
                ],
                completeness_pct=0.0,
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


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


class WeatherService:
    def __init__(
        self,
        provider: WeatherProvider,
        cache: WeatherCache,
        *,
        now: Callable[[], datetime] | None = None,
        fallback_provider: WeatherProvider | None = None,
        debug: bool = False,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.fallback_provider = fallback_provider
        self.debug = debug

    @property
    def providers(self) -> tuple[WeatherProvider, ...]:
        if self.fallback_provider is None or self.fallback_provider is self.provider:
            return (self.provider,)
        return (self.provider, self.fallback_provider)

    @staticmethod
    def _timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception as error:
            raise ValueError("timezone is not installed on this server") from error

    @staticmethod
    def _mark_stale(days: list[DailyWeather]) -> list[DailyWeather]:
        return [day.model_copy(update={"status": WeatherStatus.STALE, "stale": True}) for day in days]

    @staticmethod
    def _parse_cached(entry: WeatherCacheEntry) -> list[DailyWeather] | None:
        try:
            return [DailyWeather.model_validate(item) for item in entry.payload]
        except Exception:  # noqa: BLE001 - a corrupt cache is a miss
            return None

    @staticmethod
    def _source(forecast: list[DailyWeather], fallback: str) -> str:
        for day in forecast:
            if day.source:
                return day.source
        return fallback

    @staticmethod
    def _fetched_at(forecast: list[DailyWeather], fallback: datetime) -> datetime:
        values = [day.fetched_at for day in forecast]
        return min(values) if values else fallback

    def _diagnostics(
        self,
        provider: WeatherProvider,
        *,
        cache_state: str,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if not self.debug:
            return {}
        diagnostics = dict(getattr(provider, "last_diagnostics", {}) or {})
        diagnostics.update({"cache_state": cache_state})
        if extra:
            diagnostics.update(extra)
        return diagnostics

    @staticmethod
    def _merge_provider_forecast(
        forecast: list[DailyWeather],
        *,
        provider_name: str,
        start_date: date,
        end_date: date,
        fetched_at: datetime,
    ) -> list[DailyWeather]:
        by_date: dict[date, DailyWeather] = {}
        for day in forecast:
            if day.date < start_date or day.date > end_date:
                raise WeatherProviderError("WEATHER_PROVIDER_INVALID_RESULT")
            if day.date in by_date:
                raise WeatherProviderError("WEATHER_PROVIDER_DUPLICATE_DATE")
            by_date[day.date] = day
        current = start_date
        while current <= end_date:
            by_date.setdefault(
                current,
                DailyWeather(
                    date=current,
                    source=provider_name,
                    fetched_at=fetched_at,
                    status=WeatherStatus.NOT_AVAILABLE_YET,
                ),
            )
            current += timedelta(days=1)
        return [by_date[current] for current in sorted(by_date)]

    def _response(
        self,
        *,
        forecast: list[DailyWeather],
        provider: WeatherProvider,
        source: str,
        fetched_at: datetime,
        timezone_name: str,
        warnings: list[str],
        stale: bool = False,
        available_until: date | None = None,
        cache_state: str = "miss",
        diagnostics_extra: dict[str, object] | None = None,
    ) -> WeatherForecastResponse:
        return WeatherForecastResponse(
            status=_response_status(forecast, stale=stale),
            complete=all(day.status is WeatherStatus.OK for day in forecast),
            forecast=forecast,
            source=source,
            fetched_at=fetched_at,
            stale=stale,
            timezone=timezone_name,
            warnings=_unique(warnings),
            available_until=available_until,
            diagnostics=self._diagnostics(
                provider,
                cache_state=cache_state,
                extra=diagnostics_extra,
            ),
        )

    @staticmethod
    def _fresh_cache_is_current(
        forecast: list[DailyWeather],
        *,
        supported_end: date,
    ) -> bool:
        """Allow a horizon entry to mature instead of hiding new dates."""

        return not any(
            day.status is WeatherStatus.NOT_AVAILABLE_YET and day.date <= supported_end
            for day in forecast
        )

    async def forecast(self, request: WeatherForecastRequest) -> WeatherForecastResponse:
        timezone_info = self._timezone(request.timezone)
        raw_now = self._now()
        now = raw_now if raw_now.tzinfo else raw_now.replace(tzinfo=timezone.utc)
        now_local = now.astimezone(timezone_info)
        fetched_at = now
        errors: list[str] = []
        stale_entries: list[tuple[WeatherProvider, WeatherCacheEntry, list[DailyWeather]]] = []
        supported_ends: list[date] = []
        horizon_only = True
        horizon_error = False

        for provider in self.providers:
            provider_name = provider.name
            key = _cache_key(request, provider_name)
            supported_end = now_local.date() + timedelta(days=provider.max_forecast_days)
            try:
                cached = await asyncio.to_thread(self.cache.get, key, now=now)
            except Exception:  # noqa: BLE001 - cache failure must not block live weather
                cached = None
                errors.append("WEATHER_CACHE_READ_FAILED")
            cached_forecast = self._parse_cached(cached) if cached is not None else None
            if (
                cached is not None
                and cached_forecast is not None
                and cached.fresh
                and self._fresh_cache_is_current(
                    cached_forecast,
                    supported_end=supported_end,
                )
            ):
                return self._response(
                    forecast=cached_forecast,
                    provider=provider,
                    source=self._source(cached_forecast, provider_name),
                    fetched_at=cached.fetched_at,
                    timezone_name=request.timezone,
                    warnings=["WEATHER_CACHE_HIT"],
                    cache_state="fresh",
                    diagnostics_extra={"provider_selected": provider_name},
                )
            if cached is not None and cached_forecast is not None:
                # A fresh entry containing an old horizon still supplies the
                # last known values if the refresh fails.  It is marked stale
                # in that case rather than silently presenting it as live.
                stale_entries.append((provider, cached, cached_forecast))

            supported_ends.append(supported_end)
            if request.start_date > supported_end:
                continue
            horizon_only = False
            query_end = min(request.end_date, supported_end)
            try:
                live_forecast = await provider.daily_forecast(
                    request.lat,
                    request.lng,
                    request.start_date,
                    query_end,
                )
                live_fetched_at = self._fetched_at(live_forecast, fetched_at)
                forecast = self._merge_provider_forecast(
                    live_forecast,
                    provider_name=provider_name,
                    start_date=request.start_date,
                    end_date=query_end,
                    fetched_at=live_fetched_at,
                )
                if query_end < request.end_date:
                    forecast.extend(
                        _unknown_days(
                            query_end + timedelta(days=1),
                            request.end_date,
                            source=provider_name,
                            fetched_at=live_fetched_at,
                            status=WeatherStatus.NOT_AVAILABLE_YET,
                        )
                    )
                payload = [day.model_dump(mode="json") for day in forecast]
                try:
                    await asyncio.to_thread(
                        self.cache.put,
                        key,
                        provider=provider_name,
                        payload=payload,
                        fetched_at=live_fetched_at,
                    )
                except Exception:  # noqa: BLE001 - cache failure must not discard live weather
                    errors.append("WEATHER_CACHE_WRITE_FAILED")
                return self._response(
                    forecast=forecast,
                    provider=provider,
                    source=self._source(forecast, provider_name),
                    fetched_at=live_fetched_at,
                    timezone_name=request.timezone,
                    warnings=(
                        errors
                        + (["FORECAST_HORIZON_EXCEEDED"] if query_end < request.end_date else [])
                    ),
                    available_until=supported_end if query_end < request.end_date else None,
                    cache_state="live",
                    diagnostics_extra={"provider_selected": provider_name},
                )
            except WeatherProviderError as error:
                errors.append(error.code)
                if error.code == "KMA_WEB_FORECAST_NOT_AVAILABLE":
                    horizon_error = True

        available_until = max(supported_ends) if supported_ends else None
        if horizon_only or (horizon_error and not stale_entries):
            unknown = _unknown_days(
                request.start_date,
                request.end_date,
                source=self.provider.name,
                fetched_at=fetched_at,
                status=WeatherStatus.NOT_AVAILABLE_YET,
            )
            return WeatherForecastResponse(
                status=WeatherStatus.NOT_AVAILABLE_YET,
                complete=False,
                forecast=unknown,
                source=self.provider.name,
                fetched_at=fetched_at,
                timezone=request.timezone,
                warnings=_unique(errors + ["FORECAST_HORIZON_EXCEEDED"]),
                available_until=available_until,
                diagnostics=(
                    {"provider": self.provider.name, "cache_state": "horizon"}
                    if self.debug
                    else {}
                ),
            )

        if stale_entries:
            provider, cached, stale_forecast = stale_entries[0]
            marked = self._mark_stale(stale_forecast)
            return self._response(
                forecast=marked,
                provider=provider,
                source=self._source(marked, provider.name),
                fetched_at=cached.fetched_at,
                timezone_name=request.timezone,
                warnings=errors + ["WEATHER_STALE_CACHE"],
                stale=True,
                cache_state="stale",
                diagnostics_extra={"provider_selected": provider.name},
            )

        unknown = _unknown_days(
            request.start_date,
            request.end_date,
            source=self.provider.name,
            fetched_at=fetched_at,
            status=WeatherStatus.UNAVAILABLE,
        )
        return WeatherForecastResponse(
            status=WeatherStatus.UNAVAILABLE,
            complete=False,
            forecast=unknown,
            source=self.provider.name,
            fetched_at=fetched_at,
            timezone=request.timezone,
            warnings=_unique(errors),
            available_until=available_until,
            diagnostics=(
                {"provider": self.provider.name, "cache_state": "unavailable"}
                if self.debug
                else {}
            ),
        )

    async def status(self) -> dict[str, object]:
        api_key_configured = bool(getattr(self.provider, "api_key", None))
        return {
            "provider": self.provider.name,
            "max_forecast_days": self.provider.max_forecast_days,
            "providers": [provider.name for provider in self.providers],
            "configured": api_key_configured or self.provider.name == "kma_web",
            "api_key_configured": api_key_configured,
            "fallback_provider": (
                self.fallback_provider.name if self.fallback_provider is not None else None
            ),
            "debug": self.debug,
        }
