"""VisitKorea public HTML source for restaurants and attractions."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from backend.places import load_places, normalize_query
from backend.places.models import PlaceCategory, PlaceRecord
from crawler.places.base import PlaceDestination, PlaceSource, SourceIssue
from crawler.places.errors import (
    PlaceSourceError,
    SourceHTTPError,
    SourcePageChangedError,
    SourceParseError,
    SourceTimeoutError,
)
from crawler.places.parser import parse_place_detail, parse_search_cards


BASE_URL = "https://english.visitkorea.or.kr"
SEARCH_URL = f"{BASE_URL}/totalSearch/search.do"
SOURCE_NAME = "visitkorea"


class VisitKoreaSource(PlaceSource):
    """Collect source-native records from VisitKorea's rendered public pages."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_results: int = 6,
        request_interval_seconds: float = 0.25,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self.max_results = max_results
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self._client_factory = client_factory or self._default_client

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds)),
            follow_redirects=True,
            headers={"Accept-Language": "en-US,en;q=0.8"},
        )

    async def search(
        self,
        destination: PlaceDestination,
        category: str,
        radius_km: float,
    ) -> list[PlaceRecord]:
        self.last_issues = []
        try:
            category_enum = PlaceCategory(category)
        except ValueError as error:
            raise SourceParseError(f"Unsupported place category: {category}") from error

        search_term = _canonical_search_term(destination)
        async with self._client_factory() as client:
            response = await self._get(client, SEARCH_URL, params={"kwd": search_term})
            try:
                cards = parse_search_cards(
                    response.text,
                    category_enum,
                    base_url=BASE_URL,
                )
            except SourcePageChangedError:
                raise
            except Exception as error:
                raise SourceParseError("VisitKorea search page could not be parsed.") from error

            records: list[PlaceRecord] = []
            for card in cards[: self.max_results]:
                try:
                    detail_response = await self._get(client, card.source_url)
                    fetched_at = datetime.now(timezone.utc)
                    record = parse_place_detail(
                        detail_response.text,
                        category_enum,
                        source=SOURCE_NAME,
                        source_id=card.source_id,
                        source_url=card.source_url,
                        fetched_at=fetched_at,
                        fallback_name=card.name,
                        raw_category=card.raw_category,
                    )
                    if category_enum == PlaceCategory.ATTRACTION and _is_non_attraction(record):
                        continue
                    if record.lat is not None and record.lng is not None:
                        if _distance_km(
                            destination.lat,
                            destination.lng,
                            record.lat,
                            record.lng,
                        ) > radius_km:
                            continue
                    records.append(record)
                except PlaceSourceError as error:
                    self.last_issues.append(
                        SourceIssue(
                            code=error.code,
                            message=error.public_message,
                            retriable=error.retriable,
                        )
                    )
                except Exception:
                    self.last_issues.append(
                        SourceIssue(
                            code="PARSER_ERROR",
                            message="A public place detail page could not be parsed.",
                            retriable=False,
                        )
                    )
                if self.request_interval_seconds:
                    await asyncio.sleep(self.request_interval_seconds)
            return records

    async def _get(
        self,
        client: Any,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = await client.get(url, params=params)
        except httpx.TimeoutException as error:
            raise SourceTimeoutError(str(error)) from error
        except httpx.HTTPError as error:
            raise PlaceSourceError(str(error)) from error
        status_code = int(getattr(response, "status_code", 0))
        if status_code < 200 or status_code >= 300:
            raise SourceHTTPError(status_code)
        return response


def _canonical_search_term(destination: PlaceDestination) -> str:
    """Use the bundled city label as a source query, without geocoding."""

    normalized = normalize_query(destination.label)
    for place in load_places():
        candidates = [place.name, *place.aliases]
        if any(normalized == normalize_query(candidate) for candidate in candidates):
            for alias in place.aliases:
                if alias.isascii() and alias.strip():
                    return alias.strip()
            return place.name
    return destination.label.strip()


def _distance_km(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    radius = 6_371.0088
    lat_a_rad, lat_b_rad = math.radians(lat_a), math.radians(lat_b)
    delta_lat = math.radians(lat_b - lat_a)
    delta_lng = math.radians(lng_b - lng_a)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a_rad) * math.cos(lat_b_rad) * math.sin(delta_lng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(haversine))


def _is_non_attraction(record: PlaceRecord) -> bool:
    """Exclude obvious medical listings leaked into the source's activity tab."""

    searchable = " ".join(
        [
            record.name,
            record.subcategory or "",
            *(record.tags or []),
        ]
    ).casefold()
    return any(
        term in searchable
        for term in ("clinic", "hospital", "dental", "medical", "치과", "병원", "의료", "안과")
    )


__all__ = ["BASE_URL", "SEARCH_URL", "SOURCE_NAME", "VisitKoreaSource"]
