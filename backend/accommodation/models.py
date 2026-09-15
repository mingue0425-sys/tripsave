"""Canonical accommodation place and date-bound offer contracts for V0.6."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.models import Location
from backend.place_models import PlaceSourceRecord


class PlaceRecord(PlaceSourceRecord):
    """One source-preserved accommodation place record.

    This is deliberately not a global place entity.  ``source_id`` has meaning
    only inside ``source`` and may be absent when the public page does not
    expose a stable identifier.
    """

    category: Literal["accommodation"] = "accommodation"


class AccommodationPriceBasis(str, Enum):
    """The unit represented by an accommodation offer price."""

    PER_NIGHT = "PER_NIGHT"
    TOTAL_STAY = "TOTAL_STAY"
    UNKNOWN = "UNKNOWN"


class AccommodationOffer(BaseModel):
    """One date/occupancy-bound offer kept separate from place metadata."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: str = Field(min_length=1, max_length=120)
    source_offer_id: str | None = Field(default=None, max_length=240)

    place_source_id: str | None = Field(default=None, max_length=240)

    checkin: date
    checkout: date
    adults: int = Field(strict=True, ge=1, le=20)
    children: int = Field(default=0, strict=True, ge=0, le=20)

    room_name: str | None = Field(default=None, max_length=400)

    base_price_krw: int | None = Field(default=None, strict=True, ge=1)
    taxes_krw: int | None = Field(default=None, strict=True, ge=0)
    final_price_krw: int | None = Field(default=None, strict=True, ge=1)

    price_basis: AccommodationPriceBasis = AccommodationPriceBasis.UNKNOWN
    price_freshness: Literal["fresh", "stale", "expired", "unknown"] = "unknown"
    expires_at: datetime | None = None

    availability: bool | None = None
    fetched_at: datetime

    @field_validator("price_basis", mode="before")
    @classmethod
    def normalize_price_basis(cls, value: object) -> object:
        if isinstance(value, AccommodationPriceBasis):
            return value
        if isinstance(value, str):
            normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
            aliases = {
                "PER_NIGHT": AccommodationPriceBasis.PER_NIGHT,
                "TOTAL_STAY": AccommodationPriceBasis.TOTAL_STAY,
                "UNKNOWN": AccommodationPriceBasis.UNKNOWN,
            }
            return aliases.get(normalized, value)
        return value

    @model_validator(mode="after")
    def validate_price_breakdown(self) -> AccommodationOffer:
        if self.checkout <= self.checkin:
            raise ValueError("checkout must be after checkin.")
        if self.taxes_krw is not None and self.base_price_krw is None:
            raise ValueError("taxes_krw needs an explicit base_price_krw.")
        if (
            self.base_price_krw is not None
            and self.taxes_krw is not None
            and self.final_price_krw is not None
            and self.final_price_krw != self.base_price_krw + self.taxes_krw
        ):
            raise ValueError("final_price_krw must equal base plus taxes when both are known.")
        if self.expires_at is not None and self.expires_at <= self.fetched_at:
            raise ValueError("expires_at must be after fetched_at.")
        if self.price_freshness in {"fresh", "stale", "expired"} and self.final_price_krw is None:
            raise ValueError("a freshness state requires a numeric final price.")
        return self


class AccommodationResult(BaseModel):
    """A source record and its offers for the requested stay."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    place: PlaceRecord
    offers: list[AccommodationOffer] = Field(min_length=1, max_length=20)
    distance_km: float | None = Field(default=None, strict=True, ge=0)
    distance_text: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_offer_identity(self) -> AccommodationResult:
        for offer in self.offers:
            if (
                self.place.source_id is not None
                and offer.place_source_id is not None
                and offer.place_source_id != self.place.source_id
            ):
                raise ValueError("offer place_source_id does not match its place.")
        return self


class AccommodationSearchRequest(BaseModel):
    """Public API request; destination coordinates are retained even when a
    regional source must use the supplied label for its search UI.
    """

    model_config = ConfigDict(extra="forbid")

    destination: Location
    checkin: date
    checkout: date
    adults: int = Field(strict=True, ge=1, le=20)
    children: int = Field(default=0, strict=True, ge=0, le=20)
    radius_km: float = Field(default=20.0, strict=True, gt=0, le=50)

    @model_validator(mode="after")
    def validate_stay_dates(self) -> AccommodationSearchRequest:
        if self.checkout <= self.checkin:
            raise ValueError("checkout must be after checkin.")
        return self


class AccommodationSourceIssue(BaseModel):
    """A safe, stable failure description without raw source HTML."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)


class AccommodationSearchResponse(BaseModel):
    """API envelope; numeric price fields remain null when unavailable."""

    model_config = ConfigDict(extra="forbid")

    complete: bool
    results: list[AccommodationResult] = Field(default_factory=list)
    source_status: Literal["fresh", "cache", "empty", "partial", "unavailable"]
    cache_hit: bool = False
    fetched_at: datetime
    issues: list[AccommodationSourceIssue] = Field(default_factory=list)
