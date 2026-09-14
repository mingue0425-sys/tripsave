"""Interfaces shared by restaurant and attraction source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class PlaceDestination:
    """Destination passed to a source; no geocoder is involved."""

    lat: float
    lng: float
    label: str


@dataclass(frozen=True)
class SourceIssue:
    """A source-local issue that can be translated into the API response."""

    code: str
    message: str
    retriable: bool = False


class PlaceSource(ABC):
    """Async adapter contract for a normal public web source."""

    name = "place-source"

    def __init__(self) -> None:
        self.last_issues: list[SourceIssue] = []

    @abstractmethod
    async def search(
        self,
        destination: PlaceDestination,
        category: str,
        radius_km: float,
    ) -> list[object]:
        """Return source records for one category and regional radius."""
