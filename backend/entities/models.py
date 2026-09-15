"""Pydantic contracts for the V0.8 entity-resolution layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.accommodation.models import AccommodationOffer
from backend.place_models import PlaceSourceRecord

MATCHER_VERSION = "v0.8.0"


class MatchDecision(str, Enum):
    """The resolver never silently collapses an ambiguous candidate."""

    MATCH = "MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    NO_MATCH = "NO_MATCH"


class EntitySourceRecord(PlaceSourceRecord):
    """An input record with source-native fields kept alongside common fields.

    ``extra='allow'`` is intentional at this derived boundary: a source may
    add metadata before its raw table schema catches up.  The common V0.6/V0.7
    contract remains strict; the resolver merely carries unknown raw values
    forward instead of discarding them.
    """

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    subcategory: str | None = Field(default=None, max_length=200)
    raw_category: str | None = Field(default=None, max_length=200)
    opening_information: str | None = Field(default=None, max_length=2_000)
    tags: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("source_url must be an HTTPS URL without credentials or fragments")
        return value

    @model_validator(mode="after")
    def validate_review_count_bound(self) -> EntitySourceRecord:
        # A billion reviews is already beyond any credible single source row;
        # this guard makes absurd values quarantineable without changing the
        # raw V0.6/V0.7 schema.
        if self.review_count is not None and self.review_count > 1_000_000_000:
            raise ValueError("review_count is outside the supported source range")
        if self.source_url:
            allowed_hosts = {
                "booking": {"booking.com", "www.booking.com"},
                "visitkorea": {"english.visitkorea.or.kr"},
            }.get(self.source.casefold())
            if allowed_hosts is not None and (urlsplit(self.source_url).hostname or "").casefold() not in allowed_hosts:
                raise ValueError("source_url host is not approved for this source adapter")
        return self


class SourceMembership(BaseModel):
    """A lossless link from one canonical place to one source record."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: str = Field(min_length=1, max_length=120)
    source_id: str | None = Field(default=None, max_length=300)
    source_url: str | None = Field(default=None, max_length=2_000)
    source_record_id: str = Field(min_length=1, max_length=120)
    source_record_fingerprint: str = Field(min_length=1, max_length=128)
    match_score: float = Field(ge=0.0, le=1.0)
    match_method: str = Field(min_length=1, max_length=120)
    source_confidence: float = Field(ge=0.0, le=1.0)
    merged_at: datetime


class MatchTrace(BaseModel):
    """Explainable pairwise evidence retained inside a canonical cluster."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    record_a: str = Field(min_length=1, max_length=120)
    record_b: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0.0, le=1.0)
    decision: MatchDecision
    method: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=500)
    name_score: float = Field(ge=0.0, le=1.0)
    address_score: float | None = Field(default=None, ge=0.0, le=1.0)
    coordinate_score: float | None = Field(default=None, ge=0.0, le=1.0)
    category_score: float = Field(ge=0.0, le=1.0)
    source_identity_score: float = Field(ge=0.0, le=1.0)
    distance_m: float | None = Field(default=None, ge=0.0)
    created_at: datetime


class EntityMatchCandidate(BaseModel):
    """Persisted candidate decision, including rejected and ambiguous pairs."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    record_a: str = Field(min_length=1, max_length=120)
    record_b: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0.0, le=1.0)
    decision: MatchDecision
    reason: str = Field(min_length=1, max_length=500)
    matcher_version: str = Field(min_length=1, max_length=40)
    created_at: datetime
    name_score: float = Field(ge=0.0, le=1.0)
    address_score: float | None = Field(default=None, ge=0.0, le=1.0)
    coordinate_score: float | None = Field(default=None, ge=0.0, le=1.0)
    category_score: float = Field(ge=0.0, le=1.0)
    source_identity_score: float = Field(ge=0.0, le=1.0)
    distance_m: float | None = Field(default=None, ge=0.0)
    method: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def sort_pair(self) -> EntityMatchCandidate:
        if self.record_a > self.record_b:
            self.record_a, self.record_b = self.record_b, self.record_a
        return self


class CanonicalPlace(BaseModel):
    """Derived, stable place entity.  Raw source records remain authoritative."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=40)

    lat: float | None = Field(default=None, strict=True, ge=-90.0, le=90.0)
    lng: float | None = Field(default=None, strict=True, ge=-180.0, le=180.0)
    address: str | None = Field(default=None, max_length=1_000)

    normalized_rating: float | None = Field(default=None, ge=0.0, le=1.0)
    rating_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # This field is intentionally named for API compatibility.  It is an
    # observed sum across sources, not a de-duplicated unique-review count.
    review_count_total: int | None = Field(default=None, ge=0)
    observed_review_count_sum: int | None = Field(default=None, ge=0)
    review_count_semantics: Literal["observed_sum_not_unique"] = "observed_sum_not_unique"

    source_count: int = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[SourceMembership] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)

    raw_fields: dict[str, list[Any]] = Field(default_factory=dict)
    coordinate_conflict: bool = False
    rating_disagreement: bool = False
    decision_trace: list[MatchTrace] = Field(default_factory=list)
    matcher_version: str = Field(default=MATCHER_VERSION, min_length=1, max_length=40)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_coordinate_pair_and_review_alias(self) -> CanonicalPlace:
        if (self.lat is None) != (self.lng is None):
            raise ValueError("canonical lat and lng must be supplied together")
        if self.review_count_total is None and self.observed_review_count_sum is not None:
            self.review_count_total = self.observed_review_count_sum
        elif self.observed_review_count_sum is None and self.review_count_total is not None:
            self.observed_review_count_sum = self.review_count_total
        if self.review_count_total != self.observed_review_count_sum:
            raise ValueError("review count aliases must have the same observed sum")
        return self


class QuarantinedRecord(BaseModel):
    """Input rejected by validation; no synthetic replacement is created."""

    model_config = ConfigDict(extra="forbid")

    input_index: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class EntityResolverStats(BaseModel):
    """Deterministic counters suitable for debug/API output."""

    model_config = ConfigDict(extra="forbid")

    input_records: int = Field(ge=0)
    valid_records: int = Field(ge=0)
    quarantined_records: int = Field(ge=0)
    candidate_pairs: int = Field(ge=0)
    matched_pairs: int = Field(ge=0)
    ambiguous_pairs: int = Field(ge=0)
    rejected_pairs: int = Field(ge=0)
    canonical_count: int = Field(ge=0)
    multi_source_count: int = Field(ge=0)
    single_source_count: int = Field(ge=0)


class EntityResolveRequest(BaseModel):
    """Development/API input; this endpoint only resolves supplied records."""

    model_config = ConfigDict(extra="forbid")

    records: list[EntitySourceRecord] = Field(max_length=10_000)
    offers: list[AccommodationOffer] = Field(default_factory=list, max_length=20_000)


class EntityResolveResponse(BaseModel):
    """Canonical output plus offers and all explainability diagnostics."""

    model_config = ConfigDict(extra="forbid")

    canonical_places: list[CanonicalPlace]
    candidates: list[EntityMatchCandidate]
    # This is a validated snapshot for rebuild/debug tooling.  The existing
    # V0.6/V0.7 raw cache tables remain the source of truth; this field avoids
    # dropping source-native values when a caller resolves an in-memory batch.
    source_records: dict[str, EntitySourceRecord] = Field(default_factory=dict)
    offers_by_canonical_id: dict[str, list[AccommodationOffer]] = Field(default_factory=dict)
    unlinked_offers: list[AccommodationOffer] = Field(default_factory=list)
    quarantined: list[QuarantinedRecord] = Field(default_factory=list)
    stats: EntityResolverStats


__all__ = [
    "MATCHER_VERSION",
    "CanonicalPlace",
    "EntityMatchCandidate",
    "EntityResolveRequest",
    "EntityResolveResponse",
    "EntityResolverStats",
    "EntitySourceRecord",
    "MatchDecision",
    "MatchTrace",
    "QuarantinedRecord",
    "SourceMembership",
]
