"""Async POI service preserving empty versus unavailable states."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

from .models import PoiSearchRequest, PoiSearchResponse
from .repository import PoiIndexUnavailableError, PoiRepository


class PoiService:
    source = "local_osm"

    def __init__(self, repository: PoiRepository) -> None:
        self.repository = repository

    async def search(self, request: PoiSearchRequest) -> PoiSearchResponse:
        fetched_at = datetime.now(timezone.utc)
        statuses = {category.value: "empty" for category in request.categories}
        try:
            results = await asyncio.to_thread(self.repository.search, request)
        except PoiIndexUnavailableError:
            return PoiSearchResponse(
                complete=False,
                results=[],
                category_statuses={
                    category.value: "unavailable" for category in request.categories
                },
                source=self.source,
                fetched_at=fetched_at,
                warnings=["POI_INDEX_UNAVAILABLE"],
            )
        except (OSError, TypeError, ValueError, sqlite3.Error):
            return PoiSearchResponse(
                complete=False,
                results=[],
                category_statuses={
                    category.value: "unavailable" for category in request.categories
                },
                source=self.source,
                fetched_at=fetched_at,
                warnings=["POI_INDEX_QUERY_FAILED"],
            )

        for result in results:
            statuses[result.category.value] = "ok"
        return PoiSearchResponse(
            complete=True,
            results=results,
            category_statuses=statuses,
            source=self.source,
            fetched_at=fetched_at,
        )

    async def status(self) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.status)

    def close(self) -> None:
        self.repository.close()
