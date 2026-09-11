"""Fuel-price lookup and driving-cost orchestration."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from backend.fuel.cache import FuelCacheError, FuelPriceCache
from backend.fuel.calculator import FuelCalculationError, FuelCostCalculator
from backend.fuel.errors import InvalidFuelRequestError
from backend.fuel.models import (
    DrivingCostLeg,
    DrivingCostRequest,
    DrivingCostResponse,
    DrivingCostResult,
    FuelCalculationRequest,
    FuelCostResult,
    FuelFailureCode,
    FuelPriceResult,
    FuelResponse,
    FuelType,
)
from backend.fuel.official import (
    FuelSourceError,
    OfficialFuelPriceSource,
    source_url_for,
)
from backend.geo import haversine_distance_meters
from backend.models import Location
from backend.routing.identity import make_route_id
from backend.routing.models import RouteRequest, RouteResult
from backend.tolls.models import TollCalculationRequest, TollResponse, TollResult
from config import (
    FUEL_CACHE_TTL_S,
    FUEL_LIQUID_PRICE_URL,
    FUEL_LPG_PRICE_URL,
    TOLL_INDEX_DB,
)


LOGGER = logging.getLogger(__name__)


def _failure_code(error: FuelSourceError | None) -> FuelFailureCode:
    if error is None:
        return FuelFailureCode.FUEL_PRICE_UNAVAILABLE
    try:
        return FuelFailureCode(error.code)
    except ValueError:
        return FuelFailureCode.FUEL_SOURCE_FAILED


def _route_id(request: FuelCalculationRequest) -> str:
    computed = make_route_id(request.origin, request.destination, request.route)
    if request.route_id and request.route_id != computed:
        raise InvalidFuelRequestError("route_id does not match the route payload")
    if request.route.route_id and request.route.route_id != computed:
        raise InvalidFuelRequestError("route route_id does not match its payload")
    return computed


def _route_endpoint_is_near(location: Location, coordinate: list[float]) -> bool:
    if len(coordinate) != 2:
        return False
    try:
        endpoint = Location(lat=float(coordinate[1]), lng=float(coordinate[0]))
    except (TypeError, ValueError, ValidationError):
        return False
    return haversine_distance_meters(location, endpoint) <= 5_000.0


def validate_fuel_route(request: FuelCalculationRequest) -> str:
    """Validate the route identity and geometry before using its distance."""

    try:
        RouteRequest(origin=request.origin, destination=request.destination)
    except ValidationError as error:
        raise InvalidFuelRequestError("The fuel request locations are invalid.") from error
    coordinates = request.route.geometry.coordinates
    if not _route_endpoint_is_near(request.origin, coordinates[0]):
        raise InvalidFuelRequestError("The route geometry is not bound to its origin.")
    if not _route_endpoint_is_near(request.destination, coordinates[-1]):
        raise InvalidFuelRequestError("The route geometry is not bound to its destination.")
    return _route_id(request)


class FuelPriceService:
    """Cache and public-web source policy for national average prices."""

    def __init__(
        self,
        *,
        database_path: Path = TOLL_INDEX_DB,
        cache: FuelPriceCache | None = None,
        source: OfficialFuelPriceSource | None = None,
    ) -> None:
        self.cache = cache or FuelPriceCache(database_path, ttl_s=FUEL_CACHE_TTL_S)
        self.source = source or OfficialFuelPriceSource()
        self.metrics: Counter[str] = Counter()
        self.last_diagnostics: dict[str, object] = {}
        self._lookup_lock = asyncio.Lock()

    async def status(self) -> dict[str, object]:
        return {
            "status": "ready",
            "engine": "official-web-fuel-price+sqlite-cache",
            "scope": "national_average",
            "supported_fuel_types": [fuel_type.value for fuel_type in FuelType],
            "source_policy": "cache+verified-http+playwright-fallback",
            "source_urls": {
                "gasoline": FUEL_LIQUID_PRICE_URL,
                "diesel": FUEL_LIQUID_PRICE_URL,
                "lpg": FUEL_LPG_PRICE_URL,
            },
            "cache_ttl_s": self.cache.ttl.total_seconds(),
            "metrics": dict(self.metrics),
            "last_lookup": dict(self.last_diagnostics),
        }

    @staticmethod
    def _unavailable(
        fuel_type: FuelType,
        *,
        code: FuelFailureCode,
    ) -> FuelPriceResult:
        return FuelPriceResult(
            fuel_type=fuel_type,
            source_url=source_url_for(fuel_type),
            source_status="unavailable",
            complete=False,
            reason=code,
        )

    @staticmethod
    def _validate_source_result(
        fuel_type: FuelType, result: FuelPriceResult
    ) -> FuelPriceResult:
        if (
            result.fuel_type is not fuel_type
            or result.scope != "national_average"
            or result.region is not None
            or result.complete is not True
            or result.price_krw_per_l is None
            or not math.isfinite(result.price_krw_per_l)
            or result.price_krw_per_l <= 0
        ):
            raise FuelSourceError("The fuel source returned an invalid price result.")
        return result

    async def get_price(self, fuel_type: FuelType) -> FuelPriceResult:
        started = time.perf_counter()
        async with self._lookup_lock:
            try:
                cached = self.cache.get(fuel_type)
            except FuelCacheError as error:
                self.metrics["cache_error"] += 1
                LOGGER.warning("Fuel cache read failed: %s", error)
                cached = None
            if cached is not None:
                self.metrics["cache_hit"] += 1
                result = cached.result
                self.last_diagnostics = {
                    "fuel_type": fuel_type.value,
                    "scope": result.scope,
                    "cache_hit": True,
                    "source_status": result.source_status,
                    "price_krw_per_l": result.price_krw_per_l,
                    "fetched_at": result.fetched_at.isoformat() if result.fetched_at else None,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "crawler_status": "cache",
                }
                LOGGER.info("Fuel price cache hit diagnostics=%s", self.last_diagnostics)
                return result

            self.metrics["cache_miss"] += 1
            try:
                result = self._validate_source_result(
                    fuel_type, await self.source.get_price(fuel_type)
                )
                try:
                    self.cache.put(result)
                except FuelCacheError as error:
                    self.metrics["cache_write_error"] += 1
                    LOGGER.warning("Fuel cache write failed: %s", error)
                self.metrics["crawler_success"] += 1
                self.last_diagnostics = {
                    "fuel_type": fuel_type.value,
                    "scope": result.scope,
                    "cache_hit": False,
                    "source_status": "fresh",
                    "price_krw_per_l": result.price_krw_per_l,
                    "fetched_at": result.fetched_at.isoformat() if result.fetched_at else None,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "crawler_status": "success",
                }
                LOGGER.info("Fuel price crawler diagnostics=%s", self.last_diagnostics)
                return result
            except FuelSourceError as error:
                self.metrics["crawler_failure"] += 1
                try:
                    stale = self.cache.get(fuel_type, allow_stale=True)
                except FuelCacheError as cache_error:
                    self.metrics["cache_error"] += 1
                    LOGGER.warning("Fuel stale-cache read failed: %s", cache_error)
                    stale = None
                if stale is not None:
                    self.metrics["stale_cache_used"] += 1
                    result = stale.result
                    self.last_diagnostics = {
                        "fuel_type": fuel_type.value,
                        "scope": result.scope,
                        "cache_hit": True,
                        "source_status": "stale",
                        "price_krw_per_l": result.price_krw_per_l,
                        "fetched_at": result.fetched_at.isoformat() if result.fetched_at else None,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        "crawler_status": "stale_cache",
                        "source_error": error.code,
                    }
                    LOGGER.warning("Fuel source failed; using stale verified cache diagnostics=%s", self.last_diagnostics)
                    return result
                self.last_diagnostics = {
                    "fuel_type": fuel_type.value,
                    "scope": "national_average",
                    "cache_hit": False,
                    "source_status": "unavailable",
                    "price_krw_per_l": None,
                    "fetched_at": None,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "crawler_status": "failed",
                    "source_error": error.code,
                }
                LOGGER.warning("Fuel price unavailable diagnostics=%s", self.last_diagnostics)
                return self._unavailable(fuel_type, code=_failure_code(error))

    @staticmethod
    def _incomplete_cost(
        request: FuelCalculationRequest, price: FuelPriceResult
    ) -> FuelCostResult:
        return FuelCostResult(
            complete=False,
            fuel_type=request.fuel_type,
            fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
            distance_km=request.route.distance_m / 1000.0,
            confidence="unknown",
            reason=price.reason or FuelFailureCode.FUEL_PRICE_UNAVAILABLE,
        )

    async def calculate(
        self,
        request: FuelCalculationRequest,
        *,
        round_trip_distance_m: float | None = None,
        round_trip_distance_mode: str = "doubled_one_way",
    ) -> FuelCostResult:
        validate_fuel_route(request)
        price = await self.get_price(request.fuel_type)
        if not price.complete:
            return self._incomplete_cost(request, price)
        try:
            return FuelCostCalculator.calculate(
                distance_m=request.route.distance_m,
                fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
                price=price,
                round_trip_distance_m=round_trip_distance_m,
                round_trip_distance_mode=round_trip_distance_mode,
            )
        except FuelCalculationError as error:
            LOGGER.error("Fuel arithmetic failed after validated input: %s", error)
            return self._incomplete_cost(
                request,
                price.model_copy(
                    update={
                        "complete": False,
                        "price_krw_per_l": None,
                        "source_status": "unavailable",
                        "reason": FuelFailureCode.FUEL_PRICE_UNAVAILABLE,
                    }
                ),
            )

    async def calculate_with_price(
        self,
        request: FuelCalculationRequest,
        price: FuelPriceResult,
        *,
        round_trip_distance_m: float | None = None,
        round_trip_distance_mode: str = "doubled_one_way",
    ) -> FuelCostResult:
        validate_fuel_route(request)
        if not price.complete:
            return self._incomplete_cost(request, price)
        return FuelCostCalculator.calculate(
            distance_m=request.route.distance_m,
            fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
            price=price,
            round_trip_distance_m=round_trip_distance_m,
            round_trip_distance_mode=round_trip_distance_mode,
        )


def _confidence_for_components(
    *,
    fuel: FuelCostResult,
    toll: TollResult | None,
    complete: bool,
) -> str:
    if not complete:
        return "unknown"
    if fuel.confidence == "stale" or (toll is not None and toll.source_status == "stale"):
        return "stale"
    if fuel.complete and toll is not None and toll.complete:
        return "estimated"
    return "unknown"


def _leg(
    *,
    fuel_krw: int | None,
    toll_krw: int | None,
    fuel: FuelCostResult,
    toll: TollResult | None,
) -> DrivingCostLeg:
    complete = fuel_krw is not None and toll_krw is not None
    if complete:
        return DrivingCostLeg(
            complete=True,
            fuel_krw=fuel_krw,
            toll_krw=toll_krw,
            total_krw=fuel_krw + toll_krw,
            confidence=_confidence_for_components(fuel=fuel, toll=toll, complete=True),
        )
    known = [value for value in (fuel_krw, toll_krw) if value is not None]
    return DrivingCostLeg(
        complete=False,
        fuel_krw=fuel_krw,
        toll_krw=toll_krw,
        known_minimum_krw=sum(known) if known else None,
        confidence="unknown",
    )


class DrivingCostService:
    """Combine the existing toll engine with fuel price and route distance."""

    def __init__(self, *, toll_calculator, fuel_service: FuelPriceService, routing_client=None) -> None:
        self.toll_calculator = toll_calculator
        self.fuel_service = fuel_service
        self.routing_client = routing_client

    @staticmethod
    def _toll_request(request: DrivingCostRequest) -> TollCalculationRequest:
        return TollCalculationRequest(
            route_id=request.route_id,
            origin=request.origin,
            destination=request.destination,
            route=request.route,
            vehicle_class=request.vehicle_class,
        )

    @staticmethod
    def _toll_result(value: TollResponse | TollResult) -> TollResult:
        """Accept the existing TollCalculator response envelope and test doubles."""

        return value.toll if isinstance(value, TollResponse) else value

    async def calculate(self, request: DrivingCostRequest) -> DrivingCostResponse:
        route_id = validate_fuel_route(request)
        outbound_toll_task = asyncio.create_task(
            self.toll_calculator.calculate(self._toll_request(request))
        )
        price_task = asyncio.create_task(self.fuel_service.get_price(request.fuel_type))
        reverse_route_task = None
        if request.round_trip_mode == "directional" and self.routing_client is not None:
            reverse_route_task = asyncio.create_task(
                self.routing_client.route(request.destination, request.origin)
            )

        outbound_toll_response, price = await asyncio.gather(
            outbound_toll_task, price_task
        )
        outbound_toll = self._toll_result(outbound_toll_response)
        reverse_route: RouteResult | None = None
        reverse_route_error: Exception | None = None
        if reverse_route_task is not None:
            try:
                reverse_route = await reverse_route_task
            except Exception as error:  # return route is diagnostic, not a fake cost
                reverse_route_error = error
                LOGGER.warning("Return OSRM route unavailable; using doubled one-way mode: %s", error)

        reverse_mode = reverse_route is not None
        if reverse_mode:
            fuel = await self.fuel_service.calculate_with_price(
                request,
                price,
                round_trip_distance_m=reverse_route.distance_m,
                round_trip_distance_mode="reverse_route",
            )
            return_request = DrivingCostRequest(
                route_id=reverse_route.route_id,
                origin=request.destination,
                destination=request.origin,
                route=reverse_route,
                fuel_type=request.fuel_type,
                fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
                vehicle_class=request.vehicle_class,
                round_trip_mode="doubled_one_way",
            )
            return_toll_response = await self.toll_calculator.calculate(
                self._toll_request(return_request)
            )
            return_toll = self._toll_result(return_toll_response)
            return_toll_krw = return_toll.total_toll_krw if return_toll.complete else None
            outbound_toll_krw = outbound_toll.total_toll_krw if outbound_toll.complete else None
            one_way = _leg(
                fuel_krw=fuel.one_way_krw,
                toll_krw=outbound_toll_krw,
                fuel=fuel,
                toll=outbound_toll,
            )
            directional_toll_complete = (
                outbound_toll_krw is not None and return_toll_krw is not None
            )
            if directional_toll_complete:
                round_toll_krw = outbound_toll_krw + return_toll_krw
                round_trip_toll_mode = "directional_official"
                round_trip_toll_source = return_toll
            else:
                # The outbound official result is still useful for a
                # transparent round-trip estimate when the reverse OSM
                # journey has no provable official station pair.  Keep the
                # failed reverse result in the response, advertise the
                # fallback mode, and never synthesize an unknown toll as zero.
                round_toll_krw = outbound_toll_krw * 2 if outbound_toll_krw is not None else None
                round_trip_toll_mode = "doubled_one_way"
                round_trip_toll_source = outbound_toll
            round_trip = _leg(
                fuel_krw=fuel.round_trip_krw,
                toll_krw=round_toll_krw,
                fuel=fuel,
                toll=round_trip_toll_source if round_toll_krw is not None else None,
            )
            reason = None
            if not directional_toll_complete and outbound_toll.complete:
                reason = "RETURN_TOLL_UNAVAILABLE_USED_DOUBLED_ONE_WAY"
            if not outbound_toll.complete:
                reason = outbound_toll.reason or "TOLL_UNAVAILABLE"
            elif not fuel.complete:
                reason = fuel.reason.value if fuel.reason else "FUEL_PRICE_UNAVAILABLE"
            result = DrivingCostResult(
                complete=one_way.complete and round_trip.complete,
                one_way=one_way,
                round_trip=round_trip,
                round_trip_distance_mode="reverse_route",
                round_trip_toll_mode=round_trip_toll_mode,
                reason=reason,
            )
            return DrivingCostResponse(
                status="ok" if result.complete else "partial",
                route=request.route,
                toll=outbound_toll,
                return_toll=return_toll,
                fuel=fuel,
                driving_cost=result,
            )

        # V0.5's explicitly supported fallback is a transparent doubled
        # one-way estimate.  It is never used to convert an unavailable
        # outbound toll or fuel price into zero.
        fuel = await self.fuel_service.calculate_with_price(
            request,
            price,
            round_trip_distance_mode="doubled_one_way",
        )
        outbound_toll_krw = outbound_toll.total_toll_krw if outbound_toll.complete else None
        round_toll_krw = outbound_toll_krw * 2 if outbound_toll_krw is not None else None
        one_way = _leg(
            fuel_krw=fuel.one_way_krw,
            toll_krw=outbound_toll_krw,
            fuel=fuel,
            toll=outbound_toll,
        )
        round_trip = _leg(
            fuel_krw=fuel.round_trip_krw,
            toll_krw=round_toll_krw,
            fuel=fuel,
            toll=outbound_toll,
        )
        reason = None
        if reverse_route_error is not None:
            reason = "RETURN_ROUTE_UNAVAILABLE_USED_DOUBLED_ONE_WAY"
        if not outbound_toll.complete:
            reason = outbound_toll.reason or "TOLL_UNAVAILABLE"
        if not fuel.complete:
            reason = fuel.reason.value if fuel.reason else "FUEL_PRICE_UNAVAILABLE"
        result = DrivingCostResult(
            complete=one_way.complete and round_trip.complete,
            one_way=one_way,
            round_trip=round_trip,
            round_trip_distance_mode="doubled_one_way",
            round_trip_toll_mode="doubled_one_way",
            reason=reason,
        )
        return DrivingCostResponse(
            status="ok" if result.complete else "partial",
            route=request.route,
            toll=outbound_toll,
            fuel=fuel,
            driving_cost=result,
        )


def make_fuel_response(result: FuelCostResult) -> FuelResponse:
    return FuelResponse(status="ok" if result.complete else "partial", fuel=result)
