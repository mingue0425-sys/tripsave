"""Canonical V0.7 restaurant and attraction records."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PlaceCategory(str, Enum):
    """Categories collected by V0.7."""

    RESTAURANT = "restaurant"
    ATTRACTION = "attraction"


class PlaceDestination(BaseModel):
    """A user-selected destination used to scope regional web searches."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)
    label: str = Field(min_length=1, max_length=200)


class PlaceRecord(BaseModel):
    """A source-preserving place record.

    The rating fields deliberately retain the source scale.  No cross-source
    normalization or entity resolution is performed in V0.7.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=100)
    source_id: str | None = Field(default=None, max_length=300)
    source_url: str | None = Field(default=None, max_length=2_000)

    name: str = Field(min_length=1, max_length=500)
    category: PlaceCategory

    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lng: float | None = Field(default=None, ge=-180.0, le=180.0)
    address: str | None = Field(default=None, max_length=1_000)

    rating: float | None = Field(default=None, ge=0.0)
    rating_scale: float | None = Field(default=None, gt=0.0)
    review_count: int | None = Field(default=None, ge=0)

    fetched_at: datetime

    # Optional source-native fields.  These are not normalized taxonomies.
    subcategory: str | None = Field(default=None, max_length=200)
    raw_category: str | None = Field(default=None, max_length=200)
    opening_information: str | None = Field(default=None, max_length=2_000)
    tags: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_coordinates_and_rating(self) -> "PlaceRecord":
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be supplied together")
        if (
            self.rating is not None
            and self.rating_scale is not None
            and self.rating > self.rating_scale
        ):
            raise ValueError("rating cannot exceed rating_scale")
        return self

    @field_validator("tags")
    @classmethod
    def remove_empty_tags(cls, value: list[str]) -> list[str]:
        return [tag.strip() for tag in value if tag and tag.strip()]


class PlaceSearchRequest(BaseModel):
    """POST /api/places/search input."""

    model_config = ConfigDict(extra="forbid")

    destination: PlaceDestination
    categories: list[PlaceCategory] = Field(min_length=1, max_length=2)
    radius_km: float | None = Field(default=None, gt=0.0, le=50.0)

    @field_validator("categories")
    @classmethod
    def categories_must_be_unique(
        cls, value: list[PlaceCategory]
    ) -> list[PlaceCategory]:
        if len(set(value)) != len(value):
            raise ValueError("categories must not contain duplicates")
        return value


class PlaceSearchIssue(BaseModel):
    """A non-empty diagnostic for partial or failed source collection."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    source: str | None = Field(default=None, max_length=100)
    category: PlaceCategory | None = None
    retriable: bool = False


class PlaceSearchResponse(BaseModel):
    """Canonical API response distinguishing empty from failed searches."""

    model_config = ConfigDict(extra="forbid")

    complete: bool
    results: list[PlaceRecord]
    issues: list[PlaceSearchIssue] = Field(default_factory=list)
    cache_hit: bool = False
    source_status: Literal["fresh", "cache", "empty", "partial", "unavailable"]
    fetched_at: datetime
