"""Canonical request and response models for car routes."""

from __future__ import annotations

import math
from numbers import Real
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.geo import (
    haversine_distance_meters,
    is_within_south_korea_routing_bounds,
)
from backend.models import Location
from config import SAME_LOCATION_THRESHOLD_METERS


class RouteRequest(BaseModel):
    """A two-point car route request using explicit lat/lng objects."""

    model_config = ConfigDict(extra="forbid")

    origin: Location
    destination: Location

    @model_validator(mode="after")
    def validate_distinct_locations(self) -> "RouteRequest":
        if not is_within_south_korea_routing_bounds(self.origin):
            raise ValueError("origin is outside the South Korea routing dataset")
        if not is_within_south_korea_routing_bounds(self.destination):
            raise ValueError(
                "destination is outside the South Korea routing dataset"
            )
        distance = haversine_distance_meters(self.origin, self.destination)
        if distance <= SAME_LOCATION_THRESHOLD_METERS:
            raise ValueError(
                "origin and destination must be more than 20 metres apart"
            )
        return self


class RouteGeometry(BaseModel):
    """Validated GeoJSON LineString; coordinates are always [lng, lat]."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    type: Literal["LineString"]
    coordinates: list[list[float]] = Field(min_length=2)

    @field_validator("coordinates", mode="before")
    @classmethod
    def validate_coordinates(cls, value: object) -> list[list[float]]:
        if not isinstance(value, list) or len(value) < 2:
            raise ValueError("route geometry must contain at least two coordinates")

        normalized: list[list[float]] = []
        for coordinate in value:
            if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
                raise ValueError("each GeoJSON coordinate must be [lng, lat]")
            longitude, latitude = coordinate
            if (
                isinstance(longitude, bool)
                or isinstance(latitude, bool)
                or not isinstance(longitude, Real)
                or not isinstance(latitude, Real)
                or not math.isfinite(float(longitude))
                or not math.isfinite(float(latitude))
                or not -180 <= float(longitude) <= 180
                or not -90 <= float(latitude) <= 90
            ):
                raise ValueError("route geometry contains an invalid coordinate")
            normalized.append([float(longitude), float(latitude)])
        return normalized


class RouteResult(BaseModel):
    """Stable route result independent of the raw OSRM response shape."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    distance_m: float = Field(strict=True, gt=0)
    duration_s: float = Field(strict=True, gt=0)
    geometry: RouteGeometry
    route_id: str | None = Field(default=None, min_length=1, max_length=80)


class RouteResponse(BaseModel):
    status: Literal["ok"]
    route: RouteResult
