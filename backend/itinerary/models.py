"""V1.1 multi-stop route and cost contracts."""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.fuel.models import FuelType
from backend.geo import haversine_distance_meters
from backend.models import Location
from backend.routing.models import RouteGeometry
from backend.tolls.models import TollVehicleClass


class OptimizationMode(str, Enum):
    FASTEST = "fastest"
    SHORTEST = "shortest"
    LOWEST_COST = "lowest_cost"


class RoutePoint(BaseModel):
    """A named endpoint used by the matrix and ETA calculator."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(default="point", min_length=1, max_length=128)
    name: str = Field(default="위치", min_length=1, max_length=300)
    lat: float = Field(strict=True, ge=-90.0, le=90.0)
    lng: float = Field(strict=True, ge=-180.0, le=180.0)

    @field_validator("lat", "lng")
    @classmethod
    def coordinates_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("coordinates must be finite")
        return value


class RouteWaypoint(RoutePoint):
    category: str | None = Field(default=None, max_length=80)
    visit_duration_min: int = Field(default=0, strict=True, ge=0, le=1_440)
    fixed_time: datetime | None = None


class ItineraryVehicle(BaseModel):
    """Optional vehicle metadata for future cost-matrix providers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    fuel_type: FuelType = FuelType.GASOLINE
    efficiency_km_per_l: float = Field(strict=True, gt=0.0, le=100.0)
    vehicle_class: TollVehicleClass = TollVehicleClass.CLASS_1


class MatrixEntry(BaseModel):
    """One directed, already fetched travel matrix edge."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    from_id: str = Field(min_length=1, max_length=128)
    to_id: str = Field(min_length=1, max_length=128)
    distance_m: float = Field(strict=True, gt=0.0)
    duration_s: float = Field(strict=True, gt=0.0)
    fuel_krw: int | None = Field(default=None, strict=True, ge=0)
    toll_krw: int | None = Field(default=None, strict=True, ge=0)
    fuel_status: Literal["verified", "estimated", "unknown"] = "unknown"
    toll_status: Literal["verified", "estimated", "unknown"] = "unknown"
    source: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="before")
    @classmethod
    def infer_status_for_explicit_amounts(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if normalized.get("fuel_krw") is not None and "fuel_status" not in normalized:
            normalized["fuel_status"] = "verified"
        if normalized.get("toll_krw") is not None and "toll_status" not in normalized:
            normalized["toll_status"] = "verified"
        return normalized

    @model_validator(mode="after")
    def validate_cost_states(self) -> MatrixEntry:
        if self.from_id == self.to_id:
            raise ValueError("matrix entries cannot be self edges")
        for amount, status, label in (
            (self.fuel_krw, self.fuel_status, "fuel"),
            (self.toll_krw, self.toll_status, "toll"),
        ):
            if amount is None and status != "unknown":
                raise ValueError(f"unknown {label} amount needs unknown status")
            if amount is not None and status == "unknown":
                raise ValueError(f"numeric {label} amount needs a status")
        return self

    @property
    def cost_krw(self) -> int | None:
        if self.fuel_krw is None or self.toll_krw is None:
            return None
        return self.fuel_krw + self.toll_krw

    @property
    def cost_complete(self) -> bool:
        return self.cost_krw is not None

    @property
    def cost_is_estimated(self) -> bool:
        return self.fuel_status == "estimated" or self.toll_status == "estimated"


class OptimizeRouteRequest(BaseModel):
    """Input for POST /api/routes/optimize."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    origin: RoutePoint
    destination: RoutePoint
    waypoints: list[RouteWaypoint] = Field(default_factory=list, max_length=20)
    mode: OptimizationMode = OptimizationMode.FASTEST
    start_datetime: datetime | None = None
    max_daily_driving_min: int | None = Field(default=360, strict=True, ge=1, le=1_440)
    vehicle: ItineraryVehicle | None = None
    matrix: list[MatrixEntry] | None = Field(default=None, max_length=500)
    include_geometry: bool = True

    @model_validator(mode="before")
    @classmethod
    def supply_endpoint_ids(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field_name, default_id in (("origin", "origin"), ("destination", "destination")):
            point = normalized.get(field_name)
            if isinstance(point, dict) and not point.get("id"):
                normalized[field_name] = {**point, "id": default_id}
        return normalized

    @model_validator(mode="after")
    def validate_points(self) -> OptimizeRouteRequest:
        points: list[RoutePoint] = [self.origin, *self.waypoints, self.destination]
        ids = [point.id for point in points]
        if len(set(ids)) != len(ids):
            raise ValueError("origin, destination, and waypoints must have unique ids")
        for index, first in enumerate(points):
            for second in points[index + 1 :]:
                if haversine_distance_meters(
                    Location(lat=first.lat, lng=first.lng),
                    Location(lat=second.lat, lng=second.lng),
                ) <= 20.0:
                    raise ValueError("waypoints must not duplicate an endpoint or another waypoint")
        if any(waypoint.fixed_time is not None for waypoint in self.waypoints):
            raise ValueError("fixed_time is not supported in V1.1")
        if self.matrix is not None:
            expected_ids = set(ids)
            seen: set[tuple[str, str]] = set()
            for entry in self.matrix:
                if entry.from_id not in expected_ids or entry.to_id not in expected_ids:
                    raise ValueError("matrix entry references an unknown point")
                key = (entry.from_id, entry.to_id)
                if key in seen:
                    raise ValueError("matrix entries must be unique")
                seen.add(key)
        return self


class RouteSegment(BaseModel):
    """One chosen leg with explicit cost and ETA evidence."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    from_id: str
    to_id: str
    distance_m: float = Field(strict=True, gt=0.0)
    duration_s: float = Field(strict=True, gt=0.0)
    departure_time: datetime | None = None
    arrival_time: datetime | None = None
    next_departure_time: datetime | None = None
    visit_duration_min: int = Field(default=0, strict=True, ge=0)
    fuel_krw: int | None = Field(default=None, strict=True, ge=0)
    toll_krw: int | None = Field(default=None, strict=True, ge=0)
    fuel_status: Literal["verified", "estimated", "unknown"] = "unknown"
    toll_status: Literal["verified", "estimated", "unknown"] = "unknown"
    cost_status: Literal["verified", "estimated", "unknown"] = "unknown"
    geometry: RouteGeometry | None = None


class OptimizedRoute(BaseModel):
    """Optimization output; infeasible and incomplete are never zeroed."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: OptimizationMode
    origin: RoutePoint
    destination: RoutePoint
    ordered_waypoints: list[RouteWaypoint] = Field(default_factory=list)
    segments: list[RouteSegment] = Field(default_factory=list)
    total_distance_m: float | None = Field(default=None, strict=True, gt=0.0)
    total_duration_s: float | None = Field(default=None, strict=True, gt=0.0)
    total_cost_krw: int | None = Field(default=None, strict=True, ge=0)
    cost_complete: bool = False
    cost_status: Literal["verified", "estimated", "unknown"] = "unknown"
    geometry: RouteGeometry | None = None
    feasible: bool = False
    warnings: list[str] = Field(default_factory=list, max_length=40)
    start_datetime: datetime | None = None
    optimizer_version: str = Field(default="v1.1.0", min_length=1, max_length=40)
