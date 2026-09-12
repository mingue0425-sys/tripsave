"""Canonical fuel-price, fuel-cost, and driving-cost models for V0.5.1."""

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
    RETURN_ROUTE_UNAVAILABLE = "RETURN_ROUTE_UNAVAILABLE"


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
    """Fuel cost with explicit outbound, return, and aggregate values.

    ``complete`` describes the known outbound fuel calculation.  A separate
    ``round_trip_complete`` flag prevents an unavailable return route from
    being mistaken for a symmetric, complete trip.  The older scalar aliases
    remain for the standalone fuel endpoint, but their meanings are explicit:
    ``one_way_krw`` is outbound fuel and ``round_trip_krw`` is both legs.
    """

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
    return_distance_km: float | None = Field(default=None, strict=True, gt=0)
    return_fuel_volume_l: float | None = Field(default=None, strict=True, ge=0)
    return_fuel_cost_krw: int | None = Field(default=None, strict=True, ge=0)
    round_trip_complete: bool = False
    round_trip_distance_km: float | None = Field(default=None, strict=True, gt=0)
    round_trip_fuel_volume_l: float | None = Field(default=None, strict=True, ge=0)
    round_trip_fuel_cost_krw: int | None = Field(default=None, strict=True, ge=0)
    round_trip_krw: int | None = Field(default=None, strict=True, ge=0)
    round_trip_distance_mode: Literal["doubled_one_way", "reverse_route", "unknown"] = (
        "unknown"
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
            )
            if any(value is None for value in required):
                raise ValueError("A complete fuel cost needs outbound fuel values.")
            if self.fuel_cost_krw != self.one_way_krw:
                raise ValueError("fuel_cost_krw and one_way_krw must agree.")
            if self.price is not None and self.price_krw_per_l != self.price.price_krw_per_l:
                raise ValueError("fuel price aliases must agree.")
            if self.confidence == "unknown":
                raise ValueError("A complete fuel cost needs a non-unknown confidence.")

            if self.round_trip_complete:
                required_round_trip = (
                    self.return_distance_km,
                    self.return_fuel_volume_l,
                    self.return_fuel_cost_krw,
                    self.round_trip_distance_km,
                    self.round_trip_fuel_volume_l,
                    self.round_trip_fuel_cost_krw,
                    self.round_trip_krw,
                )
                if any(value is None for value in required_round_trip):
                    raise ValueError("A complete round-trip fuel result needs both leg values.")
                if self.round_trip_distance_mode == "unknown":
                    raise ValueError("A complete round-trip fuel result needs a distance mode.")
                if self.round_trip_distance_km != self.distance_km + self.return_distance_km:
                    raise ValueError("round-trip distance must equal outbound plus return distance.")
                if self.round_trip_fuel_volume_l != self.fuel_volume_l + self.return_fuel_volume_l:
                    raise ValueError("round-trip volume must equal outbound plus return volume.")
                if self.round_trip_fuel_cost_krw != self.fuel_cost_krw + self.return_fuel_cost_krw:
                    raise ValueError("round-trip fuel cost must equal outbound plus return cost.")
                if self.round_trip_fuel_cost_krw != self.round_trip_krw:
                    raise ValueError("round-trip fuel cost aliases must agree.")
            else:
                if any(
                    value is not None
                    for value in (
                        self.return_distance_km,
                        self.return_fuel_volume_l,
                        self.return_fuel_cost_krw,
                        self.round_trip_distance_km,
                        self.round_trip_fuel_volume_l,
                        self.round_trip_fuel_cost_krw,
                        self.round_trip_krw,
                    )
                ):
                    raise ValueError("An incomplete round-trip fuel result must not expose totals.")
        else:
            if any(
                value is not None
                for value in (
                    self.fuel_volume_l,
                    self.fuel_cost_krw,
                    self.one_way_krw,
                    self.return_distance_km,
                    self.return_fuel_volume_l,
                    self.return_fuel_cost_krw,
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
            if self.round_trip_complete:
                raise ValueError("An incomplete outbound fuel result cannot have a complete round trip.")
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
    """One explicit outbound/return leg of a driving-cost calculation."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    complete: bool
    route_id: str | None = Field(default=None, min_length=1, max_length=80)
    distance_m: float | None = Field(default=None, strict=True, gt=0)
    fuel_volume_l: float | None = Field(default=None, strict=True, ge=0)
    fuel_cost_krw: int | None = Field(default=None, strict=True, ge=0)
    toll_krw: int | None = Field(default=None, strict=True, ge=0)
    total_krw: int | None = Field(default=None, strict=True, ge=0)
    known_minimum_krw: int | None = Field(default=None, strict=True, ge=0)
    status: Literal["verified", "estimated", "unknown"] = "unknown"
    fuel_status: Literal["verified", "estimated", "unknown"] = "unknown"
    toll_status: Literal["verified", "estimated", "unknown"] = "unknown"
    toll_mode: Literal[
        "verified_official",
        "stale_official",
        "estimated_doubled_outbound",
        "unknown",
    ] = "unknown"
    toll_verified: bool = False
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_leg(self) -> "DrivingCostLeg":
        if self.complete:
            if (
                self.distance_m is None
                or self.fuel_volume_l is None
                or self.fuel_cost_krw is None
                or self.toll_krw is None
                or self.total_krw is None
            ):
                raise ValueError("A complete cost leg needs distance, fuel, toll, and total values.")
            if self.total_krw != self.fuel_cost_krw + self.toll_krw:
                raise ValueError("A complete cost leg total must equal fuel plus toll.")
            if self.known_minimum_krw is not None:
                raise ValueError("A complete cost leg has no separate minimum value.")
            if self.status == "unknown":
                raise ValueError("A complete cost leg needs a verification status.")
            if self.fuel_status == "unknown" or self.toll_status == "unknown":
                raise ValueError("A complete cost leg needs statuses for both cost components.")
            expected_status = (
                "verified"
                if self.fuel_status == "verified" and self.toll_status == "verified"
                else "estimated"
            )
            if self.status != expected_status:
                raise ValueError("A leg status must summarize its fuel and toll statuses.")
        else:
            if self.total_krw is not None:
                raise ValueError("An incomplete cost leg must not expose a total.")
            if self.status != "unknown":
                raise ValueError("An incomplete cost leg must have unknown status.")
            if self.known_minimum_krw is not None:
                expected = sum(
                    value for value in (self.fuel_cost_krw, self.toll_krw) if value is not None
                )
                if self.known_minimum_krw != expected:
                    raise ValueError("known_minimum_krw must equal the known components.")
        if self.toll_krw is None:
            if self.toll_status != "unknown" or self.toll_mode != "unknown" or self.toll_verified:
                raise ValueError("An unknown toll amount must have unknown, unverified status.")
        else:
            if self.toll_status == "unknown":
                raise ValueError("A numeric toll amount needs verified or estimated status.")
        if self.toll_verified:
            if self.toll_status != "verified" or self.toll_mode != "verified_official":
                raise ValueError("A verified toll needs verified_official mode.")
        if self.fuel_cost_krw is None and self.fuel_status != "unknown":
            raise ValueError("An unknown fuel amount must have unknown status.")
        if self.fuel_cost_krw is not None and self.fuel_status == "unknown":
            raise ValueError("A numeric fuel amount needs verified or estimated status.")
        return self

    @property
    def fuel_krw(self) -> int | None:
        """Deprecated read-only alias for callers migrating to fuel_cost_krw."""

        return self.fuel_cost_krw


class RoundTripToll(BaseModel):
    """Round-trip toll amount plus its verification state."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    amount_krw: int | None = Field(default=None, strict=True, ge=0)
    mode: Literal[
        "verified_official",
        "stale_official",
        "estimated_doubled_outbound",
        "unknown",
    ] = "unknown"
    verified: bool = False
    estimated: bool = False
    complete: bool = False
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_toll_state(self) -> "RoundTripToll":
        if self.complete != self.verified:
            raise ValueError("Toll completeness must match verification state.")
        if self.verified:
            if self.amount_krw is None or self.mode != "verified_official" or self.estimated:
                raise ValueError("A verified round-trip toll needs an official amount.")
        elif self.mode == "verified_official":
            raise ValueError("verified_official mode requires verified=true.")
        elif self.mode == "stale_official":
            if self.amount_krw is None or self.estimated or self.complete:
                raise ValueError("A stale official toll is not current verification.")
        if self.estimated:
            if self.amount_krw is None or self.mode != "estimated_doubled_outbound" or self.verified:
                raise ValueError("An estimated toll needs explicit doubled-outbound metadata.")
        if self.mode == "estimated_doubled_outbound" and not self.estimated:
            raise ValueError("estimated_doubled_outbound mode requires estimated=true.")
        if self.mode == "unknown" and self.amount_krw is not None:
            raise ValueError("An unknown toll mode must not expose an amount.")
        return self


class DrivingCostResult(BaseModel):
    """Canonical outbound/return/round-trip driving-cost aggregate."""

    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, populate_by_name=True
    )

    complete: bool
    cost_complete: bool
    outbound: DrivingCostLeg
    return_leg: DrivingCostLeg = Field(alias="return")
    round_trip: DrivingCostLeg
    round_trip_toll: RoundTripToll
    officially_verified: bool = False
    contains_estimate: bool = False
    round_trip_distance_mode: Literal["doubled_one_way", "reverse_route", "unknown"]
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_total_state(self) -> "DrivingCostResult":
        expected = self.outbound.complete and self.return_leg.complete and self.round_trip.complete
        if self.complete != expected or self.cost_complete != expected:
            raise ValueError("Driving cost completeness must reflect all canonical legs.")

        if self.round_trip.complete:
            legs = (self.outbound, self.return_leg)
            if any(leg.distance_m is None for leg in legs):
                raise ValueError("A complete round trip needs both leg distances.")
            if self.round_trip.distance_m != self.outbound.distance_m + self.return_leg.distance_m:
                raise ValueError("round-trip distance must equal outbound plus return distance.")
            if self.round_trip.fuel_volume_l != self.outbound.fuel_volume_l + self.return_leg.fuel_volume_l:
                raise ValueError("round-trip fuel volume must equal outbound plus return volume.")
            if self.round_trip.fuel_cost_krw != self.outbound.fuel_cost_krw + self.return_leg.fuel_cost_krw:
                raise ValueError("round-trip fuel cost must equal outbound plus return cost.")
            if self.round_trip.toll_krw != self.outbound.toll_krw + self.return_leg.toll_krw:
                raise ValueError("round-trip toll must equal outbound plus return toll.")
            if self.round_trip.total_krw != self.round_trip.fuel_cost_krw + self.round_trip.toll_krw:
                raise ValueError("round-trip total must equal round-trip fuel plus toll.")

        for field_name in ("distance_m", "fuel_volume_l", "fuel_cost_krw", "toll_krw"):
            outbound_value = getattr(self.outbound, field_name)
            return_value = getattr(self.return_leg, field_name)
            round_value = getattr(self.round_trip, field_name)
            if outbound_value is not None and return_value is not None and round_value is not None:
                if round_value != outbound_value + return_value:
                    raise ValueError(f"round-trip {field_name} must equal both leg values.")

        if self.round_trip_toll.amount_krw != self.round_trip.toll_krw:
            raise ValueError("round-trip toll metadata must match the aggregate toll amount.")
        if self.officially_verified:
            if not expected or self.contains_estimate or not self.round_trip_toll.verified:
                raise ValueError("An officially verified result must be complete and estimate-free.")
            if any(
                leg.status != "verified"
                for leg in (self.outbound, self.return_leg, self.round_trip)
            ):
                raise ValueError("An officially verified result needs verified canonical legs.")
        return self

    @property
    def one_way(self) -> DrivingCostLeg:
        """Deprecated read-only alias; the canonical name is ``outbound``."""

        return self.outbound

    @property
    def return_cost(self) -> DrivingCostLeg:
        """Deprecated read-only alias; the canonical name is ``return``."""

        return self.return_leg

    @property
    def round_trip_toll_mode(self) -> str:
        """Deprecated read-only alias for ``round_trip_toll.mode``."""

        return self.round_trip_toll.mode


class DrivingCostResponse(BaseModel):
    status: Literal["ok", "partial"]
    route: RouteResult
    outbound_route: RouteResult | None = None
    return_route: RouteResult | None = None
    toll: TollResult
    outbound_toll: TollResult | None = None
    return_toll: TollResult | None = None
    fuel: FuelCostResult
    driving_cost: DrivingCostResult
