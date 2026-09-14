"""Orchestration for regional restaurant and attraction collection."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Iterable

from backend.places.models import (
    PlaceCategory,
    PlaceRecord,
    PlaceSearchIssue,
    PlaceSearchRequest,
    PlaceSearchResponse,
)
from crawler.places.base import PlaceDestination, SourceIssue
from crawler.places.cache import PlaceCache, make_cache_key
from crawler.places.errors import PlaceSourceError
from crawler.places.visitkorea import VisitKoreaSource


LOGGER = logging.getLogger(__name__)

PLACE_METADATA_TTL_SECONDS = 3 * 24 * 60 * 60
PLACES_API_URL = "/api/places/search"
DEFAULT_RADIUS_KM: dict[PlaceCategory, float] = {
    PlaceCategory.RESTAURANT: 10.0,
    PlaceCategory.ATTRACTION: 30.0,
}


class PlaceService:
    """Search sources with an isolated metadata cache.

    A source failure never becomes an empty successful result.  A stale cache
    may be returned as a clearly marked partial response while a fresh source
    request is attempted.
    """

    def __init__(
        self,
        *,
        sources: Iterable[object] | None = None,
        cache: PlaceCache | None = None,
        cache_ttl_seconds: float = PLACE_METADATA_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sources = list(sources or [VisitKoreaSource()])
        self.cache = cache or PlaceCache()
        self.cache_ttl_seconds = cache_ttl_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._search_lock = asyncio.Lock()

    async def search(self, request: PlaceSearchRequest) -> PlaceSearchResponse:
        """Collect each requested category and return a canonical response."""

        async with self._search_lock:
            return await self._search_locked(request)

    async def _search_locked(self, request: PlaceSearchRequest) -> PlaceSearchResponse:
        fetched_at = self.clock()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)

        all_results: list[PlaceRecord] = []
        all_issues: list[PlaceSearchIssue] = []
        statuses: list[str] = []
        any_cache_hit = False

        for category in request.categories:
            radius_km = request.radius_km or DEFAULT_RADIUS_KM[category]
            category_results, category_issues, status, cache_hit = await self._search_category(
                request=request,
                category=category,
                radius_km=radius_km,
            )
            all_results.extend(category_results)
            all_issues.extend(category_issues)
            statuses.append(status)
            any_cache_hit = any_cache_hit or cache_hit

        if all_issues:
            source_status = "partial" if all_results else "unavailable"
        elif all_results:
            source_status = "cache" if statuses and all(s == "cache" for s in statuses) else "fresh"
        else:
            source_status = "empty"

        return PlaceSearchResponse(
            complete=not all_issues,
            results=all_results,
            issues=all_issues,
            cache_hit=any_cache_hit,
            source_status=source_status,
            fetched_at=fetched_at,
        )

    async def _search_category(
        self,
        *,
        request: PlaceSearchRequest,
        category: PlaceCategory,
        radius_km: float,
    ) -> tuple[list[PlaceRecord], list[PlaceSearchIssue], str, bool]:
        results: list[PlaceRecord] = []
        issues: list[PlaceSearchIssue] = []
        statuses: list[str] = []
        cache_hit = False

        for source in self.sources:
            source_name = str(getattr(source, "name", source.__class__.__name__.lower()))
            key = make_cache_key(
                source=source_name,
                category=category.value,
                label=request.destination.label,
                lat=request.destination.lat,
                lng=request.destination.lng,
                radius_km=radius_km,
            )
            fresh_entry = self.cache.get(key, max_age_seconds=self.cache_ttl_seconds)
            if fresh_entry is not None:
                results.extend(fresh_entry.results)
                statuses.append("cache")
                cache_hit = True
                continue

            stale_entry = self.cache.get(key)
            destination = PlaceDestination(
                lat=request.destination.lat,
                lng=request.destination.lng,
                label=request.destination.label,
            )
            try:
                live_results = await source.search(destination, category.value, radius_km)
                source_issues = list(getattr(source, "last_issues", []))
                results.extend(live_results)
                issues.extend(
                    self._convert_source_issue(issue, source_name, category)
                    for issue in source_issues
                )
                if source_issues:
                    statuses.append("partial")
                    if not live_results and stale_entry is not None:
                        results.extend(stale_entry.results)
                        cache_hit = True
                        issues.append(
                            PlaceSearchIssue(
                                code="STALE_CACHE_FALLBACK",
                                message="A stale cached result was returned while the source was partial.",
                                source=source_name,
                                category=category,
                                retriable=True,
                            )
                        )
                else:
                    self.cache.put(
                        key,
                        source=source_name,
                        category=category.value,
                        results=live_results,
                        fetched_at=self._now(),
                    )
                    statuses.append("fresh")
            except PlaceSourceError as error:
                LOGGER.info("Place source %s failed for %s: %s", source_name, category, error)
                if stale_entry is not None:
                    results.extend(stale_entry.results)
                    cache_hit = True
                    issues.append(
                        PlaceSearchIssue(
                            code="STALE_CACHE_FALLBACK",
                            message="A stale cached result was returned after source access failed.",
                            source=source_name,
                            category=category,
                            retriable=True,
                        )
                    )
                    statuses.append("partial")
                else:
                    issues.append(
                        PlaceSearchIssue(
                            code=error.code,
                            message=error.public_message,
                            source=source_name,
                            category=category,
                            retriable=error.retriable,
                        )
                    )
                    statuses.append("unavailable")
            except Exception:
                LOGGER.exception("Unexpected place source failure: %s", source_name)
                issues.append(
                    PlaceSearchIssue(
                        code="SOURCE_ERROR",
                        message="The place source returned an unexpected error.",
                        source=source_name,
                        category=category,
                        retriable=True,
                    )
                )
                statuses.append("unavailable")

        status = "partial" if issues else ("cache" if statuses and all(s == "cache" for s in statuses) else "fresh")
        if not results and not issues:
            status = "empty"
        if not statuses:
            status = "unavailable"
        return results, issues, status, cache_hit

    def _convert_source_issue(
        self, issue: SourceIssue, source: str, category: PlaceCategory
    ) -> PlaceSearchIssue:
        return PlaceSearchIssue(
            code=issue.code,
            message=issue.message,
            source=source,
            category=category,
            retriable=issue.retriable,
        )

    def _now(self) -> datetime:
        value = self.clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


places_service = PlaceService()


__all__ = [
    "DEFAULT_RADIUS_KM",
    "PLACE_METADATA_TTL_SECONDS",
    "PLACES_API_URL",
    "PlaceService",
    "places_service",
]
