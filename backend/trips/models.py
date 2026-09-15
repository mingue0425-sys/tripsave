"""V0.9 trip-candidate and cost-completeness contracts."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.accommodation.models import AccommodationOffer
from backend.entities.models import CanonicalPlace
from backend.fuel.models import DrivingCostResult, FuelType
from backend.models import Location
from backend.routing.models import RouteResult
from backend.tolls.models import TollVehicleClass

TRIP_ENGINE_VERSION = "v0.9.0"
ENTITY_RESOLVER_VERSION = "v0.8.0"


class TripType(str, Enum):
    DAY_TRIP = "DAY_TRIP"
    OVERNIGHT = "OVERNIGHT"


class CostComponentStatus(str, Enum):
    VERIFIED = "VERIFIED"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"


class TripCostStatus(str, Enum):
    VERIFIED_COMPLETE = "VERIFIED_COMPLETE"
    ESTIMATED_COMPLETE = "ESTIMATED_COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class SourceDataStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


class VehicleProfile(BaseModel):
    """The vehicle inputs required by the existing driving-cost service."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    fuel_type: FuelType = FuelType.GASOLINE
    fuel_efficiency_km_per_l: float = Field(
        alias="efficiency_km_per_l", strict=True, ge=0.1, le=100.0
    )
    vehicle_class: TollVehicleClass = TollVehicleClass.CLASS_1
    round_trip_mode: Literal["directional", "doubled_one_way"] = "directional"


class CostComponent(BaseModel):
    """One cost component with an explicit amount and evidence state."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=80)
    amount_krw: int | None = Field(default=None, strict=True, ge=0)
    status: CostComponentStatus = CostComponentStatus.UNKNOWN
    source: str | None = Field(default=None, max_length=200)
    fetched_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_amount_state(self) -> CostComponent:
        if self.amount_krw is None and self.status is not CostComponentStatus.UNKNOWN:
            raise ValueError("a cost component without an amount must be UNKNOWN")
        if self.amount_krw is not None and self.status is CostComponentStatus.UNKNOWN:
            raise ValueError("a numeric cost component needs VERIFIED or ESTIMATED status")
        return self


class TripCostBreakdown(BaseModel):
    """Known subtotal and complete total without fabricating unknown costs."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    driving_krw: int | None = Field(default=None, strict=True, ge=0)
    accommodation_krw: int | None = Field(default=None, strict=True, ge=0)
    known_subtotal_krw: int = Field(default=0, strict=True, ge=0)
    total_krw: int | None = Field(default=None, strict=True, ge=0)
    complete: bool = False
    total_cost_complete: bool = False
    status: TripCostStatus = TripCostStatus.UNKNOWN
    required_components: list[str] = Field(
        default_factory=lambda: ["driving", "accommodation"], min_length=1, max_length=10
    )
    components: list[CostComponent] = Field(default_factory=list, max_length=20)
    missing_components: list[str] = Field(default_factory=list, max_length=10)
    estimated_components: list[str] = Field(default_factory=list, max_length=20)
    verified_components: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def synthesize_scalar_components(cls, value: object) -> object:
        """Keep the scalar contract ergonomic for callers and old fixtures."""

        if not isinstance(value, dict) or "components" in value:
            return value
        components: list[dict[str, object]] = []
        for name, field_name in (("driving", "driving_krw"), ("accommodation", "accommodation_krw")):
            if field_name not in value:
                continue
            amount = value.get(field_name)
            components.append(
                {
                    "name": name,
                    "amount_krw": amount,
                    "status": "VERIFIED" if amount is not None else "UNKNOWN",
                }
            )
        if components:
            return {**value, "components": components}
        return value

    @model_validator(mode="after")
    def validate_breakdown(self) -> TripCostBreakdown:
        if len(set(self.required_components)) != len(self.required_components):
            raise ValueError("required cost component names must be unique")
        if any(not name.strip() for name in self.required_components):
            raise ValueError("required cost component names must not be blank")
        by_name: dict[str, CostComponent] = {}
        for component in self.components:
            if component.name in by_name:
                raise ValueError("cost component names must be unique")
            by_name[component.name] = component

        missing = [name for name in self.required_components if by_name.get(name, CostComponent(name=name)).amount_krw is None]
        expected_known = sum(
            component.amount_krw
            for component in self.components
            if component.amount_krw is not None
        )
        if self.known_subtotal_krw != expected_known:
            raise ValueError("known_subtotal_krw must equal the sum of known components")

        driving = by_name.get("driving")
        accommodation = by_name.get("accommodation")
        if self.driving_krw != (driving.amount_krw if driving else None):
            raise ValueError("driving_krw must agree with the driving component")
        if self.accommodation_krw != (accommodation.amount_krw if accommodation else None):
            raise ValueError("accommodation_krw must agree with the accommodation component")

        estimated = [component.name for component in self.components if component.status is CostComponentStatus.ESTIMATED]
        verified = [component.name for component in self.components if component.status is CostComponentStatus.VERIFIED]
        if self.estimated_components != estimated:
            raise ValueError("estimated_components must describe component statuses")
        if self.verified_components != verified:
            raise ValueError("verified_components must describe component statuses")
        if self.missing_components != missing:
            raise ValueError("missing_components must describe required unknown components")

        is_complete = not missing
        if self.complete != is_complete or self.total_cost_complete != is_complete:
            raise ValueError("cost completeness must reflect missing required components")
        if is_complete:
            expected_total = sum(by_name[name].amount_krw for name in self.required_components)
            if self.total_krw != expected_total:
                raise ValueError("total_krw must equal all required component amounts")
            expected_status = (
                TripCostStatus.ESTIMATED_COMPLETE
                if estimated
                else TripCostStatus.VERIFIED_COMPLETE
            )
        else:
            if self.total_krw is not None:
                raise ValueError("an incomplete cost must not expose total_krw")
            expected_status = TripCostStatus.PARTIAL if expected_known else TripCostStatus.UNKNOWN
        if self.status is not expected_status:
            raise ValueError("cost status does not match component completeness")
        return self


class TripQualityFeatures(BaseModel):
    """Descriptive features only; this model deliberately has no score."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    accommodation_rating: float | None = Field(default=None, ge=0.0, le=1.0)
    accommodation_rating_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    nearby_restaurant_count: int = Field(default=0, strict=True, ge=0)
    nearby_attraction_count: int = Field(default=0, strict=True, ge=0)
    restaurant_rating_mean: float | None = Field(default=None, ge=0.0, le=1.0)
    attraction_rating_mean: float | None = Field(default=None, ge=0.0, le=1.0)
    place_data_completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    has_accommodation: bool = False
    driving_distance_km: float | None = Field(default=None, ge=0.0)
    driving_duration_min: float | None = Field(default=None, ge=0.0)


class TripCandidateProvenance(BaseModel):
    """Dependency identities needed to explain and invalidate a candidate."""

    model_config = ConfigDict(extra="forbid")

    accommodation_canonical_id: str | None = Field(default=None, max_length=120)
    accommodation_offer_id: str | None = Field(default=None, max_length=128)
    driving_cost_route_id: str | None = Field(default=None, max_length=80)
    canonical_place_ids: list[str] = Field(default_factory=list, max_length=10_000)
    entity_resolver_version: str = Field(default=ENTITY_RESOLVER_VERSION, min_length=1, max_length=40)
    cost_engine_version: str = Field(default=TRIP_ENGINE_VERSION, min_length=1, max_length=40)
    input_fingerprint: str = Field(min_length=64, max_length=64)
    source_updated_at: datetime | None = None


class TripCandidate(BaseModel):
    """One assembled trip option, not a recommendation ranking."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=128)
    origin: Location
    destination: Location
    start_date: date
    end_date: date
    nights: int = Field(strict=True, ge=0)
    trip_type: TripType
    adults: int = Field(strict=True, ge=1, le=20)
    children: int = Field(strict=True, ge=0, le=20)

    route: RouteResult | None = None
    accommodation: CanonicalPlace | None = None
    accommodation_offer: AccommodationOffer | None = None
    restaurants: list[CanonicalPlace] = Field(default_factory=list, max_length=10_000)
    attractions: list[CanonicalPlace] = Field(default_factory=list, max_length=10_000)
    driving_cost: DrivingCostResult | None = None
    costs: TripCostBreakdown
    quality: TripQualityFeatures
    complete: bool = False
    warnings: list[str] = Field(default_factory=list, max_length=50)
    component_statuses: dict[str, SourceDataStatus] = Field(default_factory=dict)
    provenance: TripCandidateProvenance
    created_at: datetime

    @model_validator(mode="after")
    def validate_candidate_links(self) -> TripCandidate:
        expected_nights = (self.end_date - self.start_date).days
        if expected_nights != self.nights:
            raise ValueError("nights must equal checkout minus checkin")
        expected_type = TripType.DAY_TRIP if self.nights == 0 else TripType.OVERNIGHT
        if self.trip_type is not expected_type:
            raise ValueError("trip_type must agree with nights")
        if self.accommodation_offer is not None:
            if self.trip_type is TripType.DAY_TRIP:
                raise ValueError("a day trip cannot carry an accommodation offer")
            if self.accommodation is None or self.accommodation.category != "accommodation":
                raise ValueError("an accommodation offer needs a canonical accommodation")
            if self.accommodation_offer.checkin != self.start_date:
                raise ValueError("accommodation offer checkin does not match candidate")
            if self.accommodation_offer.checkout != self.end_date:
                raise ValueError("accommodation offer checkout does not match candidate")
            if self.accommodation_offer.adults != self.adults:
                raise ValueError("accommodation offer adults do not match candidate")
            if self.accommodation_offer.children != self.children:
                raise ValueError("accommodation offer children do not match candidate")
        if self.accommodation is not None and self.accommodation.category != "accommodation":
            raise ValueError("accommodation must have category accommodation")
        for place in self.restaurants:
            if place.category != "restaurant":
                raise ValueError("restaurants must contain restaurant canonical places")
        for place in self.attractions:
            if place.category != "attraction":
                raise ValueError("attractions must contain attraction canonical places")
        linked_ids = [place.id for place in [*self.restaurants, *self.attractions]]
        if len(linked_ids) != len(set(linked_ids)):
            raise ValueError("restaurant and attraction canonical IDs must be unique")
        if self.complete != self.costs.complete:
            raise ValueError("candidate completeness must equal required cost completeness")
        if self.provenance.accommodation_canonical_id != (
            self.accommodation.id if self.accommodation is not None else None
        ):
            raise ValueError("accommodation provenance does not match candidate")
        return self


class TripCandidateRequest(BaseModel):
    """API request with optional pre-fetched dependencies for deterministic assembly."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    origin: Location
    destination: Location
    start_date: date
    end_date: date
    adults: int = Field(strict=True, ge=1, le=20)
    children: int = Field(default=0, strict=True, ge=0, le=20)
    vehicle: VehicleProfile
    trip_type: TripType | None = None

    route: RouteResult | None = None
    driving_cost: DrivingCostResult | None = None
    canonical_places: list[CanonicalPlace] | None = Field(default=None, max_length=10_000)
    accommodations: list[CanonicalPlace] | None = Field(default=None, max_length=10_000)
    restaurants: list[CanonicalPlace] | None = Field(default=None, max_length=10_000)
    attractions: list[CanonicalPlace] | None = Field(default=None, max_length=10_000)
    offers: list[AccommodationOffer] | None = Field(default=None, max_length=20_000)
    source_statuses: dict[str, SourceDataStatus] = Field(default_factory=dict, max_length=20)
    source_warnings: list[str] = Field(default_factory=list, max_length=50)
    max_candidates: int = Field(default=50, strict=True, ge=1, le=200)

    @model_validator(mode="after")
    def validate_trip_dates(self) -> TripCandidateRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        expected_type = TripType.DAY_TRIP if self.end_date == self.start_date else TripType.OVERNIGHT
        if self.trip_type is not None and self.trip_type is not expected_type:
            raise ValueError("trip_type does not agree with the requested dates")
        return self


class TripCandidateResponse(BaseModel):
    """Batch response; partial dependencies remain visible to the caller."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "partial"]
    complete: bool
    candidates: list[TripCandidate] = Field(default_factory=list, max_length=200)
    component_statuses: dict[str, SourceDataStatus] = Field(default_factory=dict, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    request_fingerprint: str = Field(min_length=64, max_length=64)
    created_at: datetime

    @model_validator(mode="after")
    def validate_response_state(self) -> TripCandidateResponse:
        expected_complete = bool(self.candidates) and all(candidate.complete for candidate in self.candidates)
        if self.complete != expected_complete:
            raise ValueError("response complete must summarize candidate completeness")
        expected_status = "ok" if self.complete else "partial"
        if self.status != expected_status:
            raise ValueError("response status must summarize candidate completeness")
        return self


__all__ = [
    "ENTITY_RESOLVER_VERSION",
    "TRIP_ENGINE_VERSION",
    "CostComponent",
    "CostComponentStatus",
    "SourceDataStatus",
    "TripCandidate",
    "TripCandidateProvenance",
    "TripCandidateRequest",
    "TripCandidateResponse",
    "TripCostBreakdown",
    "TripCostStatus",
    "TripQualityFeatures",
    "TripType",
    "VehicleProfile",
]
