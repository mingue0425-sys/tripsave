"""Validated canonical location models for current and future APIs."""

from pydantic import BaseModel, ConfigDict, Field


class Location(BaseModel):
    """Canonical object form; GeoJSON array order is intentionally not used."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    lat: float = Field(strict=True, ge=-90, le=90)
    lng: float = Field(strict=True, ge=-180, le=180)
    label: str | None = Field(default=None, max_length=200)
    source: str = Field(default="map", min_length=1, max_length=64)


class Place(BaseModel):
    """A searchable local place with a flat response shape."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    lat: float = Field(strict=True, ge=-90, le=90)
    lng: float = Field(strict=True, ge=-180, le=180)
    type: str = Field(default="city", min_length=1, max_length=32)
