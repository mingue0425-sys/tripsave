"""Source adapter contract for accommodation crawlers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Protocol

from backend.accommodation.models import AccommodationResult
from backend.models import Location


class AccommodationSource(Protocol):
    """A source adapter that reads a normal public search page."""

    source_name: str

    async def search(
        self,
        destination: Location,
        checkin: date,
        checkout: date,
        adults: int,
        children: int = 0,
    ) -> list[AccommodationResult]:
        ...

    async def close(self) -> None:
        ...


SourceFactory = Callable[[], AccommodationSource]
