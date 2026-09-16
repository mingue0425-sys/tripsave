"""Validated POI contracts kept separate from V0.7 content places."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from numbers import Real
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.models import Location
from backend.routing.models import RouteResult

POI_CATEGORY_NAMESPACE = "poi"


class PoiCategory(str, Enum):
    HOSPITAL = "hospital"
    EMERGENCY = "emergency"
    CONVENIENCE_STORE = "convenience_store"
    PORT = "port"
    PASSENGER_TERMINAL = "passenger_terminal"
    FUEL_STATION = "fuel_station"
    PHARMACY = "pharmacy"
    PARKING = "parking"
    EV_CHARGER = "ev_charger"


POI_CATEGORIES = tuple(PoiCategory)
POI_CATEGORY_LABELS: dict[PoiCategory, str] = {
    PoiCategory.HOSPITAL: "병원",
    PoiCategory.EMERGENCY: "응급실",
    PoiCategory.CONVENIENCE_STORE: "편의점",
    PoiCategory.PORT: "항구",
    PoiCategory.PASSENGER_TERMINAL: "여객터미널",
    PoiCategory.FUEL_STATION: "주유소",
    PoiCategory.PHARMACY: "약국",
    PoiCategory.PARKING: "주차장",
    PoiCategory.EV_CHARGER: "EV 충전소",
}


def namespaced_category(category: PoiCategory | str) -> str:
    """Return the explicit POI namespace used at integration boundaries."""

    value = category.value if isinstance(category, PoiCategory) else str(category)
    return f"{POI_CATEGORY_NAMESPACE}:{value.removeprefix(f'{POI_CATEGORY_NAMESPACE}:')}"


def _finite_coordinate(value: object, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError("coordinate must be a finite number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError("coordinate is outside its valid range")
    return number


class PoiRecord(BaseModel):
    """One source POI record with explicit coordinates and provenance."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=120)
    source_id: str | None = Field(default=None, max_length=160)
    name: str = Field(min_length=1, max_length=300)
    category: PoiCategory
    lat: float = Field(strict=True, ge=-90.0, le=90.0)
    lng: float = Field(strict=True, ge=-180.0, le=180.0)
    address: str | None = Field(default=None, max_length=1_000)
    distance_to_destination_m: float | None = Field(default=None, strict=True, ge=0.0)
    distance_to_route_m: float | None = Field(default=None, strict=True, ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category_namespace(cls, value: object) -> object:
        if isinstance(value, str):
            return value.removeprefix(f"{POI_CATEGORY_NAMESPACE}:")
        return value

    @property
    def namespaced_category(self) -> str:
        return namespaced_category(self.category)

    @field_validator("lat")
    @classmethod
    def validate_latitude(cls, value: float) -> float:
        return _finite_coordinate(value, minimum=-90.0, maximum=90.0)

    @field_validator("lng")
    @classmethod
    def validate_longitude(cls, value: float) -> float:
        return _finite_coordinate(value, minimum=-180.0, maximum=180.0)


class PoiSearchRequest(BaseModel):
    """Destination and optional route-corridor POI query."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    destination: Location
    route: RouteResult | None = None
    categories: list[PoiCategory] = Field(
        default_factory=lambda: list(POI_CATEGORIES), min_length=1, max_length=9
    )
    destination_radius_m: float = Field(default=3_000.0, strict=True, gt=0.0, le=50_000.0)
    route_corridor_m: float = Field(default=1_000.0, strict=True, gt=0.0, le=10_000.0)
    limit_per_category: int = Field(default=50, strict=True, ge=1, le=200)

    @field_validator("categories", mode="before")
    @classmethod
    def normalize_categories(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        for item in value:
            raw = item.value if isinstance(item, PoiCategory) else str(item)
            prefix = f"{POI_CATEGORY_NAMESPACE}:"
            normalized.append(raw.removeprefix(prefix))
        return normalized

    @field_validator("categories")
    @classmethod
    def categories_must_be_unique(cls, value: list[PoiCategory]) -> list[PoiCategory]:
        if len(set(value)) != len(value):
            raise ValueError("categories must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_route_geometry(self) -> PoiSearchRequest:
        if self.route is not None and len(self.route.geometry.coordinates) < 2:
            raise ValueError("route geometry must contain at least two coordinates")
        return self


class PoiSearchResponse(BaseModel):
    """Response where successful empty and unavailable are different states."""

    model_config = ConfigDict(extra="forbid")

    complete: bool
    results: list[PoiRecord] = Field(default_factory=list)
    category_statuses: dict[str, Literal["ok", "empty", "partial", "unavailable"]]
    source: str = Field(min_length=1, max_length=120)
    fetched_at: datetime
    warnings: list[str] = Field(default_factory=list, max_length=20)
