"""Weather provider boundary; only the fixed official KMA endpoint is used."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from config import WEATHER_KMA_API_KEY, WEATHER_KMA_API_URL, WEATHER_TIMEZONE

from .models import DailyWeather, WeatherStatus
from .location import _grid_coordinates
from .parser import normalize_condition


class WeatherProviderError(RuntimeError):
    def __init__(self, code: str, *, phase: str | None = None) -> None:
        self.code = code
        self.phase = phase
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

    async def close(self) -> None:
        pass


def _condition(sky: str | None, precipitation_type: str | None) -> str | None:
    precipitation = {
        "1": "비",
        "2": "비/눈",
        "3": "눈",
        "4": "소나기",
        "5": "빗방울",
        "6": "빗방울/눈날림",
        "7": "눈날림",
    }.get(precipitation_type)
    if precipitation is not None:
        return normalize_condition(precipitation)
    return normalize_condition({"1": "맑음", "3": "구름 많음", "4": "흐림"}.get(sky))


def _parse_precipitation_mm(raw: str) -> float | None:
    """Parse only exact API precipitation amounts without inventing a midpoint."""

    value = raw.strip().replace(" ", "")
    if value in {"강수없음", "없음", "0", "0.0", "0mm", "0.0mm"}:
        return 0.0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:mm)?", value, flags=re.IGNORECASE)
    if match is None:
        return None
    return float(match.group(1))


def _daily_condition(
    sky_entries: list[tuple[str, str]],
    precipitation_entries: list[tuple[str, str]],
) -> str | None:
    """Choose a deterministic travel-day condition from hourly API values."""

    active = {
        value
        for _forecast_time, value in precipitation_entries
        if value not in {"", "0"}
    }
    rainish = active & {"1", "4", "5"}
    snowish = active & {"3", "7"}
    if "2" in active or "6" in active or (rainish and snowish):
        return "비/눈"
    if snowish:
        return "눈"
    if "1" in active:
        return "비"
    if "4" in active:
        return "소나기"
    if "5" in active:
        return "빗방울"

    def time_value(value: str) -> int | None:
        if len(value) != 4 or not value.isdigit():
            return None
        return int(value)

    daytime = [
        sky
        for forecast_time, sky in sky_entries
        if (parsed := time_value(forecast_time)) is not None and 900 <= parsed <= 1800
    ]
    candidates = daytime or [sky for _forecast_time, sky in sky_entries]
    candidates = [sky for sky in candidates if sky in {"1", "3", "4"}]
    if not candidates:
        return None
    counts = Counter(candidates)
    severity = {"1": 0, "3": 1, "4": 2}
    representative = max(counts, key=lambda code: (counts[code], severity[code]))
    return _condition(representative, "0")


class KmaApiWeatherProvider:
    """Short-range KMA API adapter retained as the API-first provider."""

    name = "kma_public"
    max_forecast_days = 3

    def __init__(self, api_key: str | None = WEATHER_KMA_API_KEY, *, timeout_s: float = 10.0) -> None:
        self.api_key = api_key
        self.timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0))

    @staticmethod
    def _base_time(now: datetime) -> tuple[str, str]:
        # Avoid selecting a cycle at the exact nominal issue minute: the
        # public product can lag the base time briefly.  A small publication
        # delay prevents transient empty/error responses around each cycle.
        effective_now = now - timedelta(minutes=15)
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
        local_hour = effective_now.hour
        selected = (23, "2300")
        for hour, base_time in cycles:
            if local_hour >= hour:
                selected = (hour, base_time)
        base_date = effective_now.date()
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

        grouped: dict[date, dict[str, list[tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                item_date = datetime.strptime(str(item["fcstDate"]), "%Y%m%d").replace(
                    tzinfo=timezone.utc
                ).date()
                category = str(item["category"])
                value = str(item["fcstValue"])
                forecast_time = str(item.get("fcstTime", "")).zfill(4)
            except (KeyError, ValueError):
                continue
            if start_date <= item_date <= end_date:
                grouped[item_date][category].append((forecast_time, value))

        fetched_at = datetime.now(timezone.utc)
        result: list[DailyWeather] = []
        current = start_date
        while current <= end_date:
            values = grouped.get(current, {})
            def numbers(
                category: str,
                minimum: float | None = None,
                maximum: float | None = None,
                day_values=values,
            ) -> list[float]:
                parsed: list[float] = []
                for _forecast_time, raw in day_values.get(category, []):
                    try:
                        value = float(raw)
                    except ValueError:
                        continue
                    if (
                        not math.isfinite(value)
                        or minimum is not None
                        and value < minimum
                        or maximum is not None
                        and value > maximum
                    ):
                        continue
                    parsed.append(value)
                return parsed

            temps = numbers("TMP", -100.0, 70.0)
            min_temps = numbers("TMN", -100.0, 70.0)
            max_temps = numbers("TMX", -100.0, 70.0)
            pops = numbers("POP", 0.0, 100.0)
            wind = numbers("WSD", 0.0)
            humidity = numbers("REH", 0.0, 100.0)
            sky_values = values.get("SKY", [])
            pty_values = values.get("PTY", [])
            precipitation_entries = values.get("PCP", [])
            parsed_precipitation = [
                _parse_precipitation_mm(raw)
                for _forecast_time, raw in precipitation_entries
            ]
            precipitation_mm = (
                sum(value for value in parsed_precipitation if value is not None)
                if precipitation_entries
                and all(value is not None for value in parsed_precipitation)
                else None
            )
            precipitation_text = "; ".join(
                dict.fromkeys(raw for _forecast_time, raw in precipitation_entries)
            ) or None
            condition = _daily_condition(sky_values, pty_values)
            temp_min = min_temps[-1] if min_temps else min(temps) if temps else None
            temp_max = max_temps[-1] if max_temps else max(temps) if temps else None
            if temp_min is not None and temp_max is not None and temp_min > temp_max:
                raise WeatherProviderError("WEATHER_PARSE_FAILED")
            known_values = {
                "condition": condition,
                "temp_min_c": temp_min,
                "temp_max_c": temp_max,
                "precipitation_probability_pct": max(pops) if pops else None,
                "precipitation": precipitation_mm is not None,
                "wind_speed_mps": max(wind) if wind else None,
            }
            known_fields = [
                name
                for name, value in known_values.items()
                if value is not None and value is not False
            ]
            missing_fields = [name for name in known_values if name not in known_fields]
            result.append(
                DailyWeather(
                    date=current,
                    condition=condition,
                    raw_condition=condition,
                    normalized_condition=condition,
                    temp_min_c=temp_min,
                    temp_max_c=temp_max,
                    precipitation_probability_pct=max(pops) if pops else None,
                    precipitation_mm=precipitation_mm,
                    precipitation_text=precipitation_text,
                    wind_speed_mps=max(wind) if wind else None,
                    humidity_pct=sum(humidity) / len(humidity) if humidity else None,
                    source=self.name,
                    source_url=WEATHER_KMA_API_URL,
                    known_fields=known_fields,
                    missing_fields=missing_fields,
                    completeness_pct=round(len(known_fields) / len(known_values) * 100.0, 1),
                    fetched_at=fetched_at,
                    status=(
                        WeatherStatus.OK
                        if not missing_fields
                        else WeatherStatus.PARTIAL
                    ),
                )
            )
            current += timedelta(days=1)
        return result

    async def close(self) -> None:
        # API requests use a short-lived client for compatibility with the
        # existing adapter; there is no persistent resource to release.
        pass


# Backwards-compatible import name used by existing integrations.
KmaWeatherProvider = KmaApiWeatherProvider


__all__ = [
    "KmaApiWeatherProvider",
    "KmaWeatherProvider",
    "UnavailableWeatherProvider",
    "WeatherProvider",
    "WeatherProviderError",
    "_grid_coordinates",
]
