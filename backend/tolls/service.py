"""Toll calculation orchestration from canonical route to TollResult."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from backend.geo import haversine_distance_meters
from backend.models import Location
from backend.routing.identity import make_route_id
from backend.routing.models import RouteRequest
from backend.tolls.cache import TollCacheError, TollRateCache
from backend.tolls.errors import (
    InvalidTollRequestError,
    TollIndexServiceUnavailableError,
)
from backend.tolls.index import TollIndex, TollIndexUnavailableError
from backend.tolls.models import (
    TollCalculationRequest,
    TollJourney,
    TollResponse,
    TollResult,
)
from backend.tolls.names import normalize_toll_name, official_query_name
from backend.tolls.official import (
    KoreaExpresswayTollCrawler,
    OfficialTollError,
    OfficialTollLookup,
)
from config import (
    OFFICIAL_TOLL_URL,
    TOLL_CACHE_TTL_DAYS,
    TOLL_INDEX_DB,
    TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_M,
    TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_RATIO,
)


LOGGER = logging.getLogger(__name__)


class TollCalculator:
    """Coordinate OSM evidence, official lookup, caching, and normalization."""

    def __init__(
        self,
        *,
        index_path: Path = TOLL_INDEX_DB,
        cache: TollRateCache | None = None,
        crawler: KoreaExpresswayTollCrawler | None = None,
    ) -> None:
        self.index = TollIndex(index_path)
        self.cache = cache or TollRateCache(index_path, ttl_days=TOLL_CACHE_TTL_DAYS)
        self.crawler = crawler or KoreaExpresswayTollCrawler(source_url=OFFICIAL_TOLL_URL)
        self._lookup_lock = asyncio.Lock()
        self.metrics: Counter[str] = Counter()

    async def status(self) -> dict[str, object]:
        ready = self.index.ready
        result: dict[str, object] = {
            "status": "ready" if ready else "unavailable",
            "engine": "osm+tollgate-index+official-web",
            "profile": "car",
            "local_index": ready,
            "official_source": OFFICIAL_TOLL_URL,
            "source_policy": "normal_html_only",
            "metrics": dict(self.metrics),
        }
        if not ready:
            result["message"] = (
                "Local OSM toll index is unavailable. "
                "Run scripts/build_tollgate_index.py."
            )
        return result

    @staticmethod
    def _route_id(request: TollCalculationRequest) -> str:
        computed = make_route_id(request.origin, request.destination, request.route)
        if request.route_id and request.route_id != computed:
            raise InvalidTollRequestError("route_id does not match the route payload")
        if request.route.route_id and request.route.route_id != computed:
            raise InvalidTollRequestError("route route_id does not match its payload")
        return computed

    @staticmethod
    def _route_endpoint_is_near(location: Location, coordinate: list[float]) -> bool:
        if len(coordinate) != 2:
            return False
        try:
            endpoint = Location(lat=float(coordinate[1]), lng=float(coordinate[0]))
        except (TypeError, ValueError, ValidationError):
            return False
        return haversine_distance_meters(location, endpoint) <= 5_000.0

    @classmethod
    def _validate_route_binding(cls, request: TollCalculationRequest) -> None:
        # Reuse the V0.3 request guard so toll requests cannot bypass the
        # South Korea bounds or same-location policy.
        try:
            RouteRequest(origin=request.origin, destination=request.destination)
        except ValidationError as error:
            raise InvalidTollRequestError("The toll request locations are invalid.") from error
        coordinates = request.route.geometry.coordinates
        if not cls._route_endpoint_is_near(request.origin, coordinates[0]):
            raise InvalidTollRequestError(
                "The route geometry is not bound to its requested origin."
            )
        if not cls._route_endpoint_is_near(request.destination, coordinates[-1]):
            raise InvalidTollRequestError(
                "The route geometry is not bound to its requested destination."
            )

    @staticmethod
    def _operator(analysis) -> str | None:
        operators = [road.operator for road in analysis.toll_roads if road.operator]
        if not operators:
            return None
        return Counter(operators).most_common(1)[0][0]

    @staticmethod
    def _partial_result(
        *,
        request: TollCalculationRequest,
        route_id: str,
        reason: str,
        detected_gates,
        known_toll_krw: int | None = None,
        unknown_segments: int = 1,
    ) -> TollResult:
        return TollResult(
            status="partial",
            complete=False,
            vehicle_class=request.vehicle_class,
            total_toll_krw=None,
            known_toll_krw=known_toll_krw,
            journeys=[],
            detected_toll_gates=detected_gates,
            unknown_segments=unknown_segments,
            reason=reason,
            source_status="unavailable",
            route_id=route_id,
        )

    async def _lookup_with_cache(
        self,
        entry_name: str,
        exit_name: str,
    ) -> tuple[OfficialTollLookup | None, str, datetime | None, str | None]:
        try:
            fresh = self.cache.get(entry_name, exit_name)
        except TollCacheError as error:
            LOGGER.warning("Toll cache read failed: %s", error)
            fresh = None
        if fresh is not None:
            self.metrics["cache_hit"] += 1
            return fresh.lookup, "fresh", fresh.lookup.fetched_at, None

        self.metrics["cache_miss"] += 1
        async with self._lookup_lock:
            # A second caller may have populated the cache while waiting.
            try:
                fresh = self.cache.get(entry_name, exit_name)
            except TollCacheError as error:
                LOGGER.warning("Toll cache recheck failed: %s", error)
                fresh = None
            if fresh is not None:
                self.metrics["cache_hit_after_lock"] += 1
                return fresh.lookup, "fresh", fresh.lookup.fetched_at, None
            try:
                stale = self.cache.get(entry_name, exit_name, allow_stale=True)
            except TollCacheError as error:
                LOGGER.warning("Toll stale-cache read failed: %s", error)
                stale = None
            try:
                lookup = await self.crawler.lookup(entry_name, exit_name)
                try:
                    self.cache.put(entry_name, exit_name, lookup)
                except TollCacheError as error:
                    # The official result is still usable in this response;
                    # do not turn a cache-only write failure into free toll.
                    LOGGER.warning("Toll cache write failed: %s", error)
                self.metrics["crawl_success"] += 1
                return lookup, "fresh", lookup.fetched_at, None
            except OfficialTollError as error:
                self.metrics["crawl_failure"] += 1
                if stale is not None:
                    self.metrics["stale_cache_used"] += 1
                    return (
                        stale.lookup,
                        "stale",
                        stale.lookup.fetched_at,
                        error.code,
                    )
                return None, "unavailable", None, error.code

    def _distance_is_consistent(self, route_distance_m: float, lookup: OfficialTollLookup) -> bool:
        if lookup.distance_km is None:
            return True
        official_distance_m = lookup.distance_km * 1000.0
        mismatch = abs(route_distance_m - official_distance_m)
        allowed = max(
            TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_M,
            route_distance_m * TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_RATIO,
        )
        return mismatch <= allowed

    @staticmethod
    def _official_pair_is_consistent(
        entry_name: str,
        exit_name: str,
        lookup: OfficialTollLookup,
    ) -> bool:
        requested_entry = normalize_toll_name(entry_name)
        requested_exit = normalize_toll_name(exit_name)
        return (
            requested_entry == normalize_toll_name(lookup.entry_name)
            and requested_exit == normalize_toll_name(lookup.exit_name)
        )

    async def calculate(self, request: TollCalculationRequest) -> TollResponse:
        self._validate_route_binding(request)
        route_id = self._route_id(request)

        coordinates = [tuple(point) for point in request.route.geometry.coordinates]
        try:
            analysis = self.index.analyze_route(coordinates)
        except TollIndexUnavailableError as error:
            self.metrics["index_unavailable"] += 1
            raise TollIndexServiceUnavailableError(str(error)) from error

        if not analysis.toll_road_detected and not analysis.gates:
            self.metrics["toll_free_result"] += 1
            result = TollResult(
                status="ok",
                complete=True,
                vehicle_class=request.vehicle_class,
                total_toll_krw=0,
                known_toll_krw=0,
                journeys=[],
                detected_toll_gates=[],
                unknown_segments=0,
                reason="no_detected_toll_road",
                source_status="not_applicable",
                fetched_at=datetime.now(timezone.utc),
                route_id=route_id,
            )
            return TollResponse(status="ok", toll=result)

        if analysis.unsupported_private_road:
            self.metrics["private_toll_road"] += 1
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason="unsupported_private_toll_segment",
                detected_gates=analysis.gates,
            )
            return TollResponse(status="partial", toll=result)

        if analysis.unknown_toll_operator:
            self.metrics["unknown_toll_operator"] += 1
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason="unknown_toll_operator",
                detected_gates=analysis.gates,
            )
            return TollResponse(status="partial", toll=result)

        if len(analysis.gates) < 2:
            self.metrics["gate_match_failure"] += 1
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason="toll_entry_exit_not_identified",
                detected_gates=analysis.gates,
            )
            return TollResponse(status="partial", toll=result)

        entry_gate = analysis.gates[0].gate
        exit_gate = analysis.gates[-1].gate
        entry_name = official_query_name(entry_gate.name)
        exit_name = official_query_name(exit_gate.name)
        if not entry_name or not exit_name or normalize_toll_name(entry_name) == normalize_toll_name(exit_name):
            self.metrics["gate_name_failure"] += 1
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason="toll_entry_exit_names_ambiguous",
                detected_gates=analysis.gates,
            )
            return TollResponse(status="partial", toll=result)

        lookup, source_status, fetched_at, source_error = await self._lookup_with_cache(
            entry_name, exit_name
        )
        if lookup is None:
            self.metrics["official_lookup_failure"] += 1
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason=source_error or "official_toll_lookup_failed",
                detected_gates=analysis.gates,
            )
            return TollResponse(status="partial", toll=result)

        if not self._official_pair_is_consistent(entry_name, exit_name, lookup):
            self.metrics["official_pair_mismatch"] += 1
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason="TOLL_ROUTE_MISMATCH",
                detected_gates=analysis.gates,
            )
            return TollResponse(status="partial", toll=result)

        if not self._distance_is_consistent(request.route.distance_m, lookup):
            self.metrics["official_distance_mismatch"] += 1
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason="TOLL_ROUTE_MISMATCH",
                detected_gates=analysis.gates,
            )
            return TollResponse(status="partial", toll=result)

        try:
            amount = lookup.prices[request.vehicle_class]
        except KeyError:
            self.metrics["vehicle_price_missing"] += 1
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason="official_vehicle_class_price_missing",
                detected_gates=analysis.gates,
            )
            return TollResponse(status="partial", toll=result)

        journey = TollJourney(
            entry=entry_gate,
            exit=exit_gate,
            operator=self._operator(analysis),
            route_distance_m=request.route.distance_m,
            toll_krw=amount,
            source=lookup.source,
            confidence="verified",
            source_status=source_status,
            fetched_at=fetched_at,
        )
        result = TollResult(
            status="ok",
            complete=True,
            vehicle_class=request.vehicle_class,
            total_toll_krw=amount,
            known_toll_krw=amount,
            journeys=[journey],
            detected_toll_gates=analysis.gates,
            unknown_segments=0,
            reason="official_web_verified" if source_status == "fresh" else "official_web_stale_cache",
            source_status=source_status,
            fetched_at=fetched_at,
            route_id=route_id,
        )
        return TollResponse(status="ok", toll=result)
