"""Canonical fuel-price, fuel-cost, and driving-cost models for V0.5."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models import Location
from backend.routing.models import RouteResult
from backend.tolls.models import TollResult, TollVehicleClass


class FuelType(str, Enum):
    GASOLINE = "gasoline"
    DIESEL = "diesel"
    LPG = "lpg"


class FuelPriceUnit(str, Enum):
    KRW_PER_L = "krw_per_l"


class FuelFailureCode(str, Enum):
    FUEL_PRICE_UNAVAILABLE = "FUEL_PRICE_UNAVAILABLE"
    FUEL_SOURCE_FAILED = "FUEL_SOURCE_FAILED"
    FUEL_SOURCE_TIMEOUT = "FUEL_SOURCE_TIMEOUT"
    FUEL_ACCESS_DENIED = "FUEL_ACCESS_DENIED"
    FUEL_PAGE_CHANGED = "FUEL_PAGE_CHANGED"
    FUEL_PARSE_FAILED = "FUEL_PARSE_FAILED"
    FUEL_UNIT_UNSUPPORTED = "FUEL_UNIT_UNSUPPORTED"


FUEL_TYPE_LABELS: dict[FuelType, str] = {
    FuelType.GASOLINE: "휘발유",
    FuelType.DIESEL: "경유",
    FuelType.LPG: "LPG",
}


class FuelPriceResult(BaseModel):
    """One verified public-web fuel price, or an explicit unavailable state."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    fuel_type: FuelType
    price_krw_per_l: float | None = Field(default=None, strict=True, ge=0)
    unit: FuelPriceUnit = FuelPriceUnit.KRW_PER_L
    scope: Literal["national_average"] = "national_average"
    region: None = None
    source: str = Field(default="opinet_web", min_length=1, max_length=120)
    source_url: str = Field(min_length=1, max_length=500)
    observed_at: datetime | None = None
    fetched_at: datetime | None = None
    expires_at: datetime | None = None
    source_status: Literal["fresh", "stale", "unavailable"] = "unavailable"
    cache_hit: bool = False
    complete: bool = False
    reason: FuelFailureCode | None = None
    raw_evidence_hash: str | None = Field(default=None, min_length=64, max_length=64)
    parser_version: str = Field(default="opinet-average-price-v1", min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_price_state(self) -> "FuelPriceResult":
        if self.complete:
            if self.price_krw_per_l is None or self.price_krw_per_l <= 0:
                raise ValueError("A complete fuel price needs a positive price per litre.")
            if self.source_status == "unavailable":
                raise ValueError("A complete fuel price cannot be unavailable.")
        elif self.price_krw_per_l is not None:
            raise ValueError("An unavailable fuel price must not expose a numeric price.")
        return self


class FuelCostResult(BaseModel):
    """Fuel cost derived from one canonical route and one public-web price."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    complete: bool
    fuel_type: FuelType
    fuel_efficiency_km_per_l: float = Field(strict=True, ge=0.1, le=100.0)
    price: FuelPriceResult | None = None
    price_krw_per_l: float | None = Field(default=None, strict=True, ge=0)
    distance_km: float = Field(strict=True, gt=0)
    fuel_volume_l: float | None = Field(default=None, strict=True, ge=0)
    fuel_cost_krw: int | None = Field(default=None, strict=True, ge=0)
    one_way_krw: int | None = Field(default=None, strict=True, ge=0)
    round_trip_distance_km: float | None = Field(default=None, strict=True, gt=0)
    round_trip_fuel_volume_l: float | None = Field(default=None, strict=True, ge=0)
    round_trip_fuel_cost_krw: int | None = Field(default=None, strict=True, ge=0)
    round_trip_krw: int | None = Field(default=None, strict=True, ge=0)
    round_trip_distance_mode: Literal["doubled_one_way", "reverse_route"] = (
        "doubled_one_way"
    )
    confidence: Literal["estimated", "stale", "unknown"] = "unknown"
    reason: FuelFailureCode | None = None

    @model_validator(mode="after")
    def validate_cost_state(self) -> "FuelCostResult":
        if self.complete:
            required = (
                self.price,
                self.price_krw_per_l,
                self.fuel_volume_l,
                self.fuel_cost_krw,
                self.one_way_krw,
                self.round_trip_distance_km,
                self.round_trip_fuel_volume_l,
                self.round_trip_fuel_cost_krw,
                self.round_trip_krw,
            )
            if any(value is None for value in required):
                raise ValueError("A complete fuel cost needs both one-way and round-trip values.")
            if self.fuel_cost_krw != self.one_way_krw:
                raise ValueError("fuel_cost_krw and one_way_krw must agree.")
            if self.round_trip_fuel_cost_krw != self.round_trip_krw:
                raise ValueError("round-trip fuel cost aliases must agree.")
            if self.price is not None and self.price_krw_per_l != self.price.price_krw_per_l:
                raise ValueError("fuel price aliases must agree.")
            if self.confidence == "unknown":
                raise ValueError("A complete fuel cost needs a non-unknown confidence.")
        else:
            if any(
                value is not None
                for value in (
                    self.fuel_volume_l,
                    self.fuel_cost_krw,
                    self.one_way_krw,
                    self.round_trip_distance_km,
                    self.round_trip_fuel_volume_l,
                    self.round_trip_fuel_cost_krw,
                    self.round_trip_krw,
                )
            ):
                raise ValueError("An incomplete fuel cost must not expose a numeric cost.")
            if self.price is not None or self.price_krw_per_l is not None:
                raise ValueError("An incomplete fuel cost must not expose a fuel price.")
            if self.confidence != "unknown":
                raise ValueError("An incomplete fuel cost must have unknown confidence.")
        return self


class FuelCalculationRequest(BaseModel):
    """Fuel calculation bound to the same canonical route as the toll API."""

    model_config = ConfigDict(extra="forbid")

    route_id: str | None = Field(default=None, min_length=1, max_length=80)
    origin: Location
    destination: Location
    route: RouteResult
    fuel_type: FuelType
    fuel_efficiency_km_per_l: float = Field(strict=True, ge=0.1, le=100.0)


class FuelResponse(BaseModel):
    status: Literal["ok", "partial"]
    fuel: FuelCostResult


class DrivingCostRequest(FuelCalculationRequest):
    """Aggregate request for outbound and return driving cost."""

    vehicle_class: TollVehicleClass = TollVehicleClass.CLASS_1
    round_trip_mode: Literal["directional", "doubled_one_way"] = "directional"


class DrivingCostLeg(BaseModel):
    """One-way or return cost leg with no fabricated total."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    complete: bool
    fuel_krw: int | None = Field(default=None, strict=True, ge=0)
    toll_krw: int | None = Field(default=None, strict=True, ge=0)
    total_krw: int | None = Field(default=None, strict=True, ge=0)
    known_minimum_krw: int | None = Field(default=None, strict=True, ge=0)
    confidence: Literal["verified", "estimated", "stale", "unknown"] = "unknown"

    @model_validator(mode="after")
    def validate_leg(self) -> "DrivingCostLeg":
        if self.complete:
            if self.fuel_krw is None or self.toll_krw is None or self.total_krw is None:
                raise ValueError("A complete cost leg needs fuel, toll, and total values.")
            if self.total_krw != self.fuel_krw + self.toll_krw:
                raise ValueError("A complete cost leg total must equal fuel plus toll.")
            if self.known_minimum_krw is not None:
                raise ValueError("A complete cost leg has no separate minimum value.")
            if self.confidence == "unknown":
                raise ValueError("A complete cost leg needs a confidence value.")
        else:
            if self.total_krw is not None:
                raise ValueError("An incomplete cost leg must not expose a total.")
            if self.known_minimum_krw is None and self.fuel_krw is None and self.toll_krw is None:
                # This is a valid completely unknown leg, but it remains explicit.
                if self.confidence != "unknown":
                    raise ValueError("A fully unknown leg must have unknown confidence.")
            if self.known_minimum_krw is not None:
                expected = sum(value for value in (self.fuel_krw, self.toll_krw) if value is not None)
                if self.known_minimum_krw != expected:
                    raise ValueError("known_minimum_krw must equal the known components.")
        return self


class DrivingCostResult(BaseModel):
    """Combined toll and fuel totals for one-way and return travel."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    complete: bool
    one_way: DrivingCostLeg
    round_trip: DrivingCostLeg
    round_trip_distance_mode: Literal["doubled_one_way", "reverse_route"]
    round_trip_toll_mode: Literal["doubled_one_way", "directional_official"]
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_total_state(self) -> "DrivingCostResult":
        expected = self.one_way.complete and self.round_trip.complete
        if self.complete != expected:
            raise ValueError("Driving cost completeness must reflect both cost legs.")
        return self


class DrivingCostResponse(BaseModel):
    status: Literal["ok", "partial"]
    route: RouteResult
    toll: TollResult
    return_toll: TollResult | None = None
    fuel: FuelCostResult
    driving_cost: DrivingCostResult
