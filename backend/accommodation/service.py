"""Accommodation search orchestration, source failure handling, and caching."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.accommodation.models import (
    AccommodationResult,
    AccommodationSearchRequest,
    AccommodationSearchResponse,
    AccommodationSourceIssue,
)
from config import ACCOMMODATION_API_URL
from crawler.accommodation.base import AccommodationSource
from crawler.accommodation.booking import BookingComSource
from crawler.accommodation.cache import AccommodationCache, AccommodationCacheError
from crawler.accommodation.errors import AccommodationSourceError

LOGGER = logging.getLogger(__name__)

__all__ = ["ACCOMMODATION_API_URL", "AccommodationService"]

ACCOMMODATION_PLACE_TTL_S = float(
    os.getenv("KTO_ACCOMMODATION_PLACE_CACHE_TTL_S", str(3 * 24 * 60 * 60))
)
ACCOMMODATION_OFFER_TTL_S = float(
    os.getenv("KTO_ACCOMMODATION_OFFER_CACHE_TTL_S", str(3 * 60 * 60))
)
ACCOMMODATION_SOURCE_TIMEOUT_S = float(
    os.getenv("KTO_ACCOMMODATION_SOURCE_TIMEOUT_S", "60")
)


_ERROR_MESSAGES = {
    "ACCOMMODATION_SOURCE_FAILED": "숙소 source 요청에 실패했습니다.",
    "ACCOMMODATION_SOURCE_TIMEOUT": "숙소 source 응답 시간이 초과되었습니다.",
    "ACCOMMODATION_ACCESS_DENIED": "숙소 source가 자동 접근을 허용하지 않았습니다.",
    "ACCOMMODATION_PAGE_CHANGED": "숙소 source 페이지 구조가 변경되었습니다.",
    "ACCOMMODATION_PARSE_FAILED": "숙소 source 결과를 해석하지 못했습니다.",
    "ACCOMMODATION_PRICE_UNAVAILABLE": "숙소 후보는 확인했지만 가격을 확인하지 못했습니다.",
    "ACCOMMODATION_DESTINATION_LABEL_REQUIRED": "숙소 source 검색에 사용할 지역명이 필요합니다.",
}


def _safe_source_message(error: BaseException) -> str:
    code = getattr(error, "code", "ACCOMMODATION_SOURCE_FAILED")
    return _ERROR_MESSAGES.get(code, _ERROR_MESSAGES["ACCOMMODATION_SOURCE_FAILED"])


def _results_have_complete_prices(results: list[AccommodationResult]) -> bool:
    """A search is complete only when every offer has a safe price or a
    source-explicit unavailable state.  A missing numeric price is never zero.
    """

    return all(
        offer.final_price_krw is not None or offer.availability is False
        for result in results
        for offer in result.offers
    )


def _price_unavailable_issue(source: str) -> AccommodationSourceIssue:
    return AccommodationSourceIssue(
        source=source,
        code="ACCOMMODATION_PRICE_UNAVAILABLE",
        message=_ERROR_MESSAGES["ACCOMMODATION_PRICE_UNAVAILABLE"],
    )


def _apply_requested_radius(
    results: list[AccommodationResult],
    request: AccommodationSearchRequest,
) -> list[AccommodationResult]:
    """Use source-reported distance when available.

    Booking currently reports distance from its searched city center rather
    than accommodation coordinates.  Keeping records without a distance is
    safer than pretending an exact geospatial filter was applied.
    """

    return [
        result
        for result in results
        if result.distance_km is None or result.distance_km <= request.radius_km
    ]


class AccommodationService:
    """Coordinate one or more source adapters without entity resolution."""

    def __init__(
        self,
        *,
        database_path: Path,
        sources: list[AccommodationSource] | None = None,
        cache: AccommodationCache | None = None,
        source_timeout_s: float = ACCOMMODATION_SOURCE_TIMEOUT_S,
    ) -> None:
        self.sources = list(sources) if sources is not None else [BookingComSource()]
        self.cache = cache or AccommodationCache(
            database_path,
            place_ttl_s=ACCOMMODATION_PLACE_TTL_S,
            offer_ttl_s=ACCOMMODATION_OFFER_TTL_S,
        )
        self.source_timeout_s = max(1.0, float(source_timeout_s))

    async def search(
        self,
        request: AccommodationSearchRequest,
    ) -> AccommodationSearchResponse:
        fetched_at = datetime.now(timezone.utc)
        if not self.sources:
            issue = AccommodationSourceIssue(
                source="accommodation",
                code="ACCOMMODATION_SOURCE_DISABLED",
                message="사용 가능한 숙소 source가 없습니다.",
            )
            return AccommodationSearchResponse(
                complete=False,
                results=[],
                source_status="unavailable",
                cache_hit=False,
                fetched_at=fetched_at,
                issues=[issue],
            )

        all_results: list[AccommodationResult] = []
        issues: list[AccommodationSourceIssue] = []
        cache_hits = 0
        all_sources_complete = True
        all_sources_cached = True

        for source in self.sources:
            source_name = str(getattr(source, "source_name", "accommodation"))
            cached = None
            try:
                cached = self.cache.get(source_name, request)
            except AccommodationCacheError as error:
                LOGGER.warning("Accommodation cache read failed: %s", error)
            if cached is not None:
                cache_hits += 1
                all_results.extend(cached.results)
                all_sources_complete = all_sources_complete and cached.complete
                if not cached.complete:
                    # Partial offer caches intentionally retain unknown prices,
                    # but the reason must remain visible after a cache hit.
                    issues.append(_price_unavailable_issue(source_name))
                continue

            all_sources_cached = False
            try:
                raw_results = await asyncio.wait_for(
                    source.search(
                        request.destination,
                        request.checkin,
                        request.checkout,
                        request.adults,
                        request.children,
                    ),
                    timeout=self.source_timeout_s,
                )
                results = [AccommodationResult.model_validate(item) for item in raw_results]
                results = _apply_requested_radius(results, request)
                source_complete = _results_have_complete_prices(results)
                all_results.extend(results)
                all_sources_complete = all_sources_complete and source_complete
                if not source_complete:
                    issues.append(_price_unavailable_issue(source_name))
                try:
                    self.cache.put(
                        source_name,
                        request,
                        results,
                        complete=source_complete,
                    )
                except AccommodationCacheError as error:
                    LOGGER.warning("Accommodation cache write failed: %s", error)
            except asyncio.TimeoutError:
                all_sources_complete = False
                issues.append(
                    AccommodationSourceIssue(
                        source=source_name,
                        code="ACCOMMODATION_SOURCE_TIMEOUT",
                        message=_ERROR_MESSAGES["ACCOMMODATION_SOURCE_TIMEOUT"],
                    )
                )
                LOGGER.warning("Accommodation source timed out: %s", source_name)
            except AccommodationSourceError as error:
                all_sources_complete = False
                code = str(getattr(error, "code", "ACCOMMODATION_SOURCE_FAILED"))
                issues.append(
                    AccommodationSourceIssue(
                        source=source_name,
                        code=code,
                        message=_safe_source_message(error),
                    )
                )
                LOGGER.warning("Accommodation source failed source=%s code=%s", source_name, code)
            except (ValidationError, TypeError, ValueError) as error:
                all_sources_complete = False
                issues.append(
                    AccommodationSourceIssue(
                        source=source_name,
                        code="ACCOMMODATION_PARSE_FAILED",
                        message=_ERROR_MESSAGES["ACCOMMODATION_PARSE_FAILED"],
                    )
                )
                LOGGER.warning("Accommodation source result validation failed: %s", error)
            except Exception:
                all_sources_complete = False
                issues.append(
                    AccommodationSourceIssue(
                        source=source_name,
                        code="ACCOMMODATION_SOURCE_FAILED",
                        message=_ERROR_MESSAGES["ACCOMMODATION_SOURCE_FAILED"],
                    )
                )
                LOGGER.exception("Unexpected accommodation source failure: %s", source_name)

        complete = all_sources_complete and not issues
        if issues:
            source_status = "partial" if all_results else "unavailable"
        elif not complete:
            source_status = "partial"
        elif not all_results:
            source_status = "empty"
        elif all_sources_cached:
            source_status = "cache"
        else:
            source_status = "fresh"
        return AccommodationSearchResponse(
            complete=complete,
            results=all_results,
            source_status=source_status,
            cache_hit=cache_hits > 0,
            fetched_at=fetched_at,
            issues=issues,
        )

    async def close(self) -> None:
        for source in self.sources:
            close = getattr(source, "close", None)
            if close is None:
                continue
            try:
                result: Any = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                LOGGER.debug("Could not close accommodation source", exc_info=True)
