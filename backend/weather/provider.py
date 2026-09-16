"""Weather provider boundary; only the fixed official KMA endpoint is used."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from config import WEATHER_KMA_API_KEY, WEATHER_KMA_API_URL, WEATHER_TIMEZONE

from .models import DailyWeather, WeatherStatus


class WeatherProviderError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WeatherProvider(Protocol):
    name: str
    max_forecast_days: int

    async def daily_forecast(
        self, lat: float, lng: float, start_date: date, end_date: date
    ) -> list[DailyWeather]: ...


class UnavailableWeatherProvider:
    name = "kma_public"
    max_forecast_days = 3

    async def daily_forecast(self, lat: float, lng: float, start_date: date, end_date: date) -> list[DailyWeather]:
        del lat, lng, start_date, end_date
        raise WeatherProviderError("WEATHER_PROVIDER_UNAVAILABLE")


def _grid_coordinates(lat: float, lng: float) -> tuple[int, int]:
    """Convert WGS84 coordinates to the KMA Lambert grid without geocoding."""

    re = 6_371.00877
    grid = 5.0
    slat1, slat2 = 30.0, 60.0
    olon, olat = 126.0, 38.0
    xo, yo = 43, 136
    deg_to_rad = math.pi / 180.0
    re /= grid
    slat1 *= deg_to_rad
    slat2 *= deg_to_rad
    olon *= deg_to_rad
    olat *= deg_to_rad
    lat_rad = lat * deg_to_rad
    lng_rad = lng * deg_to_rad
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5)
        / math.tan(math.pi * 0.25 + slat1 * 0.5)
    )
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5) ** sn * math.cos(slat1) / sn
    ro = re * sf / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    ra = re * sf / math.tan(math.pi * 0.25 + lat_rad * 0.5) ** sn
    theta = lng_rad - olon
    if theta > math.pi:
        theta -= 2 * math.pi
    if theta < -math.pi:
        theta += 2 * math.pi
    theta *= sn
    return round(ra * math.sin(theta) + xo), round(ro - ra * math.cos(theta) + yo)


def _condition(sky: str | None, precipitation_type: str | None) -> str | None:
    if precipitation_type in {"1", "4"}:
        return "비"
    if precipitation_type in {"2", "3"}:
        return "비/눈"
    return {"1": "맑음", "3": "구름 많음", "4": "흐림"}.get(sky)


class KmaWeatherProvider:
    """Short-range village forecast adapter for KMA public data."""

    name = "kma_public"
    max_forecast_days = 3

    def __init__(self, api_key: str | None = WEATHER_KMA_API_KEY, *, timeout_s: float = 10.0) -> None:
        self.api_key = api_key
        self.timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0))

    @staticmethod
    def _base_time(now: datetime) -> tuple[str, str]:
        # KMA publishes village forecasts at fixed three-hour cycles.  Select
        # the latest stable cycle; the provider still returns only requested
        # local-calendar dates.
        cycles = [
            (2, "0200"),
            (5, "0500"),
            (8, "0800"),
            (11, "1100"),
            (14, "1400"),
            (17, "1700"),
            (20, "2000"),
            (23, "2300"),
        ]
        local_hour = now.hour
        selected = (23, "2300")
        for hour, base_time in cycles:
            if local_hour >= hour:
                selected = (hour, base_time)
        base_date = now.date()
        if local_hour < 2:
            base_date -= timedelta(days=1)
        return base_date.strftime("%Y%m%d"), selected[1]

    async def daily_forecast(self, lat: float, lng: float, start_date: date, end_date: date) -> list[DailyWeather]:
        if not self.api_key:
            raise WeatherProviderError("WEATHER_API_KEY_UNCONFIGURED")
        timezone_info = ZoneInfo(WEATHER_TIMEZONE)
        now = datetime.now(timezone_info)
        base_date, base_time = self._base_time(now)
        nx, ny = _grid_coordinates(lat, lng)
        params = {
            "serviceKey": self.api_key,
            "pageNo": 1,
            "numOfRows": 1_000,
            "dataType": "JSON",
            "base_date": base_date,
            "base_time": base_time,
            "nx": nx,
            "ny": ny,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(WEATHER_KMA_API_URL, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException as error:
            raise WeatherProviderError("WEATHER_SOURCE_TIMEOUT") from error
        except (httpx.HTTPError, ValueError) as error:
            raise WeatherProviderError("WEATHER_SOURCE_FAILED") from error
        try:
            items = payload["response"]["body"]["items"]["item"]
        except (KeyError, TypeError) as error:
            raise WeatherProviderError("WEATHER_PARSE_FAILED") from error
        if not isinstance(items, list):
            raise WeatherProviderError("WEATHER_PARSE_FAILED")

        grouped: dict[date, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                item_date = datetime.strptime(str(item["fcstDate"]), "%Y%m%d").replace(
                    tzinfo=timezone.utc
                ).date()
                category = str(item["category"])
                value = str(item["fcstValue"])
            except (KeyError, ValueError):
                continue
            if start_date <= item_date <= end_date:
                grouped[item_date][category].append(value)

        fetched_at = datetime.now(timezone.utc)
        result: list[DailyWeather] = []
        current = start_date
        while current <= end_date:
            values = grouped.get(current, {})
            def numbers(category: str, day_values=values) -> list[float]:
                parsed: list[float] = []
                for raw in day_values.get(category, []):
                    try:
                        parsed.append(float(raw))
                    except ValueError:
                        continue
                return parsed

            temps = numbers("TMP")
            min_temps = numbers("TMN")
            max_temps = numbers("TMX")
            pops = numbers("POP")
            rain = numbers("PCP")
            wind = numbers("WSD")
            humidity = numbers("REH")
            sky_values = values.get("SKY", [])
            pty_values = values.get("PTY", [])
            condition = _condition(sky_values[-1] if sky_values else None, pty_values[-1] if pty_values else None)
            has_data = bool(values)
            result.append(
                DailyWeather(
                    date=current,
                    condition=condition,
                    temp_min_c=min_temps[-1] if min_temps else min(temps) if temps else None,
                    temp_max_c=max_temps[-1] if max_temps else max(temps) if temps else None,
                    precipitation_probability_pct=max(pops) if pops else None,
                    precipitation_mm=sum(value for value in rain if value >= 0) if rain else None,
                    wind_speed_mps=max(wind) if wind else None,
                    humidity_pct=sum(humidity) / len(humidity) if humidity else None,
                    source=self.name,
                    fetched_at=fetched_at,
                    status=WeatherStatus.OK if has_data else WeatherStatus.PARTIAL,
                )
            )
            current += timedelta(days=1)
        return result
