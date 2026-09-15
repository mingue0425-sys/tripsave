"""Shared source-preserving place contract for V0.6 and V0.7.

This is intentionally only the stable provenance/metadata boundary.  It does
not perform entity resolution, rating normalization, source ranking, or
recommendation scoring.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlaceSourceRecord(BaseModel):
    """Common source-native place fields shared by all collected categories."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: str = Field(min_length=1, max_length=120)
    source_id: str | None = Field(default=None, max_length=300)
    source_url: str | None = Field(default=None, max_length=2_000)

    name: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=40)

    lat: float | None = Field(default=None, strict=True, ge=-90.0, le=90.0)
    lng: float | None = Field(default=None, strict=True, ge=-180.0, le=180.0)
    address: str | None = Field(default=None, max_length=1_000)

    rating: float | None = Field(default=None, strict=True, ge=0.0)
    rating_scale: float | None = Field(default=None, strict=True, gt=0.0)
    review_count: int | None = Field(default=None, strict=True, ge=0)

    fetched_at: datetime

    @model_validator(mode="after")
    def validate_common_semantics(self) -> "PlaceSourceRecord":
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be supplied together")
        if self.rating is not None and self.rating_scale is None:
            raise ValueError("rating_scale is required when rating is present")
        if (
            self.rating is not None
            and self.rating_scale is not None
            and self.rating > self.rating_scale
        ):
            raise ValueError("rating cannot exceed rating_scale")
        return self


__all__ = ["PlaceSourceRecord"]
