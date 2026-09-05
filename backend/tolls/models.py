"""Canonical toll-domain and API models.

The routing module owns ``RouteResult``.  Toll models consume that stable
result and deliberately do not expose the Korea Expressway page's HTML shape.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models import Location
from backend.routing.models import RouteResult


class TollVehicleClass(str, Enum):
    CLASS_1 = "class_1"
    CLASS_2 = "class_2"
    CLASS_3 = "class_3"
    CLASS_4 = "class_4"
    CLASS_5 = "class_5"
    COMPACT = "compact"


VEHICLE_CLASS_LABELS: dict[TollVehicleClass, str] = {
    TollVehicleClass.CLASS_1: "1종 승용/소형차",
    TollVehicleClass.CLASS_2: "2종 중형차",
    TollVehicleClass.CLASS_3: "3종 대형차",
    TollVehicleClass.CLASS_4: "4종 대형화물차",
    TollVehicleClass.CLASS_5: "5종 특수화물차",
    TollVehicleClass.COMPACT: "1종 경차",
}


class TollGate(BaseModel):
    """An OSM-derived gate candidate; ``name`` retains the raw OSM name."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=120)
    osm_type: Literal["node", "way"]
    osm_id: int = Field(strict=True, gt=0)
    name: str | None = Field(default=None, max_length=200)
    normalized_name: str = Field(default="", max_length=200)
    lat: float = Field(strict=True, ge=-90, le=90)
    lng: float = Field(strict=True, ge=-180, le=180)
    road_name: str | None = Field(default=None, max_length=200)
    operator: str | None = Field(default=None, max_length=200)
    ref: str | None = Field(default=None, max_length=100)
    gate_type: Literal["toll_booth", "toll_gantry"]
    source: Literal["osm"] = "osm"


class MatchedTollGate(BaseModel):
    """A gate candidate projected onto the route in travel order."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    gate: TollGate
    distance_to_route_m: float = Field(strict=True, ge=0)
    position_along_route_m: float = Field(strict=True, ge=0)
    confidence: Literal["high", "medium", "low"]


class MatchedTollRoad(BaseModel):
    """An OSM ``toll=yes`` way close to the calculated route."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    osm_id: int = Field(strict=True, gt=0)
    name: str | None = Field(default=None, max_length=200)
    ref: str | None = Field(default=None, max_length=100)
    operator: str | None = Field(default=None, max_length=200)
    distance_to_route_m: float = Field(strict=True, ge=0)
    position_along_route_m: float = Field(strict=True, ge=0)
    confidence: Literal["high", "medium", "low"]


class TollAnalysis(BaseModel):
    """Validated OSM evidence before an official price lookup."""

    model_config = ConfigDict(extra="forbid")

    toll_road_detected: bool
    toll_roads: list[MatchedTollRoad] = Field(default_factory=list)
    gates: list[MatchedTollGate] = Field(default_factory=list)
    unsupported_private_road: bool = False
    unknown_toll_operator: bool = False


class TollJourney(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    entry: TollGate
    exit: TollGate
    operator: str | None = Field(default=None, max_length=200)
    route_distance_m: float | None = Field(default=None, strict=True, ge=0)
    toll_krw: int | None = Field(default=None, strict=True, ge=0)
    source: str = Field(min_length=1, max_length=100)
    confidence: Literal["verified", "inferred", "unknown"]
    source_status: Literal["fresh", "stale", "unavailable", "not_applicable"] = (
        "unavailable"
    )
    fetched_at: datetime | None = None


class TollResult(BaseModel):
    """Canonical result; unknown toll is never represented as zero."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    status: Literal["ok", "partial", "error"]
    complete: bool
    vehicle_class: TollVehicleClass
    total_toll_krw: int | None = Field(default=None, strict=True, ge=0)
    known_toll_krw: int | None = Field(default=None, strict=True, ge=0)
    journeys: list[TollJourney] = Field(default_factory=list)
    detected_toll_gates: list[MatchedTollGate] = Field(default_factory=list)
    unknown_segments: int = Field(default=0, strict=True, ge=0)
    reason: str | None = Field(default=None, max_length=200)
    source_status: Literal["fresh", "stale", "unavailable", "not_applicable"] = (
        "unavailable"
    )
    fetched_at: datetime | None = None
    route_id: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_completion(self) -> "TollResult":
        if self.complete and self.total_toll_krw is None:
            raise ValueError("a complete toll result must contain total_toll_krw")
        if not self.complete and self.total_toll_krw is not None:
            raise ValueError("an incomplete toll result must not expose a total")
        if self.status == "ok" and not self.complete:
            raise ValueError("an ok toll result must be complete")
        if self.status == "partial" and self.complete:
            raise ValueError("a partial toll result must be incomplete")
        return self


class TollCalculationRequest(BaseModel):
    """A toll request bound to an already calculated canonical route."""

    model_config = ConfigDict(extra="forbid")

    route_id: str | None = Field(default=None, min_length=1, max_length=80)
    origin: Location
    destination: Location
    route: RouteResult
    vehicle_class: TollVehicleClass = TollVehicleClass.CLASS_1

    @model_validator(mode="after")
    def validate_route_identity(self) -> "TollCalculationRequest":
        if self.route_id and self.route.route_id and self.route_id != self.route.route_id:
            raise ValueError("route_id does not match the supplied route")
        return self


class TollResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "partial"]
    toll: TollResult
