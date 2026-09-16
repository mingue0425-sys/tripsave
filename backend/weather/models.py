"""Weather models with unknown fields kept nullable."""

from __future__ import annotations

import math
from datetime import date, datetime
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WeatherStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    NOT_AVAILABLE_YET = "not_available_yet"
    STALE = "stale"


class DailyWeather(BaseModel):
    """One local-calendar day of provider observations/forecast."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    date: date
    condition: str | None = Field(default=None, max_length=120)
    # ``condition`` is the canonical value consumed by the UI.  Keep the
    # public-page wording as evidence so a parser/layout change never erases
    # what the user actually saw.
    raw_condition: str | None = Field(default=None, max_length=120)
    normalized_condition: str | None = Field(default=None, max_length=120)
    temp_min_c: float | None = Field(default=None, strict=True, ge=-100.0, le=70.0)
    temp_max_c: float | None = Field(default=None, strict=True, ge=-100.0, le=70.0)
    precipitation_probability_pct: float | None = Field(default=None, strict=True, ge=0.0, le=100.0)
    precipitation_mm: float | None = Field(default=None, strict=True, ge=0.0)
    precipitation_min_mm: float | None = Field(default=None, strict=True, ge=0.0)
    precipitation_max_mm: float | None = Field(default=None, strict=True, ge=0.0)
    precipitation_text: str | None = Field(default=None, max_length=160)
    wind_speed_mps: float | None = Field(default=None, strict=True, ge=0.0)
    wind_direction: str | None = Field(default=None, max_length=80)
    humidity_pct: float | None = Field(default=None, strict=True, ge=0.0, le=100.0)
    source: str = Field(min_length=1, max_length=120)
    source_url: str | None = Field(default=None, max_length=2_000)
    parser_version: str | None = Field(default=None, max_length=80)
    content_fingerprint: str | None = Field(default=None, max_length=128)
    location_name: str | None = Field(default=None, max_length=200)
    known_fields: list[str] = Field(default_factory=list, max_length=20)
    missing_fields: list[str] = Field(default_factory=list, max_length=20)
    completeness_pct: float | None = Field(default=None, strict=True, ge=0.0, le=100.0)
    fetched_at: datetime
    status: WeatherStatus = WeatherStatus.OK
    stale: bool = False

    @field_validator(
        "temp_min_c",
        "temp_max_c",
        "precipitation_probability_pct",
        "precipitation_mm",
        "precipitation_min_mm",
        "precipitation_max_mm",
        "wind_speed_mps",
        "humidity_pct",
        "completeness_pct",
    )
    @classmethod
    def finite_values_only(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("weather values must be finite")
        return value

    @model_validator(mode="after")
    def validate_temperature_order(self) -> DailyWeather:
        if self.temp_min_c is not None and self.temp_max_c is not None and self.temp_min_c > self.temp_max_c:
            raise ValueError("minimum temperature cannot exceed maximum temperature")
        if (
            self.precipitation_min_mm is not None
            and self.precipitation_max_mm is not None
            and self.precipitation_min_mm > self.precipitation_max_mm
        ):
            raise ValueError("minimum precipitation cannot exceed maximum precipitation")
        if self.precipitation_mm is not None:
            if (
                self.precipitation_min_mm is not None
                and self.precipitation_mm < self.precipitation_min_mm
            ) or (
                self.precipitation_max_mm is not None
                and self.precipitation_mm > self.precipitation_max_mm
            ):
                raise ValueError("exact precipitation must fit its declared range")
        return self


class WeatherForecastRequest(BaseModel):
    """Date-range forecast request; provider selection is server-owned."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    lat: float = Field(strict=True, ge=-90.0, le=90.0)
    lng: float = Field(strict=True, ge=-180.0, le=180.0)
    start_date: date
    end_date: date
    timezone: str = Field(default="Asia/Seoul", min_length=1, max_length=64)

    @field_validator("lat", "lng")
    @classmethod
    def coordinates_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("coordinates must be finite")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> WeatherForecastRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if (self.end_date - self.start_date).days > 31:
            raise ValueError("weather requests cannot span more than 32 days")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone is not installed on this server") from error
        return self


class WeatherForecastResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: WeatherStatus
    complete: bool
    forecast: list[DailyWeather] = Field(default_factory=list, max_length=32)
    source: str = Field(min_length=1, max_length=120)
    fetched_at: datetime
    stale: bool = False
    timezone: str = Field(min_length=1, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    available_until: date | None = None
    diagnostics: dict[str, object] = Field(default_factory=dict)
