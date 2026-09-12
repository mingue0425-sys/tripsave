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
    RoundTripToll,
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

    async def calculate_outbound_only_with_price(
        self,
        request: FuelCalculationRequest,
        price: FuelPriceResult,
        *,
        reason: str = "RETURN_ROUTE_UNAVAILABLE",
    ) -> FuelCostResult:
        """Return only the known outbound fuel when no return route exists."""

        validate_fuel_route(request)
        if not price.complete:
            return self._incomplete_cost(request, price)
        return FuelCostCalculator.calculate_outbound_only(
            distance_m=request.route.distance_m,
            fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
            price=price,
            reason=reason,
        )


def _leg(
    *,
    route_id: str | None,
    distance_m: float | None,
    fuel_volume_l: float | None,
    fuel_cost_krw: int | None,
    toll_krw: int | None,
    fuel_status: str,
    toll_mode: str = "unknown",
    toll_verified: bool = False,
    reason: str | None = None,
) -> DrivingCostLeg:
    complete = (
        distance_m is not None
        and fuel_volume_l is not None
        and fuel_cost_krw is not None
        and toll_krw is not None
    )
    toll_status = "unknown"
    if toll_krw is not None:
        toll_status = "verified" if toll_verified else "estimated"
    if complete:
        status = "verified" if fuel_status == "verified" and toll_status == "verified" else "estimated"
        return DrivingCostLeg(
            complete=True,
            route_id=route_id,
            distance_m=distance_m,
            fuel_volume_l=fuel_volume_l,
            fuel_cost_krw=fuel_cost_krw,
            toll_krw=toll_krw,
            total_krw=fuel_cost_krw + toll_krw,
            status=status,
            fuel_status=fuel_status,
            toll_status=toll_status,
            toll_mode=toll_mode,
            toll_verified=toll_verified,
            reason=reason,
        )
    known = [value for value in (fuel_cost_krw, toll_krw) if value is not None]
    return DrivingCostLeg(
        complete=False,
        route_id=route_id,
        distance_m=distance_m,
        fuel_volume_l=fuel_volume_l,
        fuel_cost_krw=fuel_cost_krw,
        toll_krw=toll_krw,
        known_minimum_krw=sum(known) if known else None,
        status="unknown",
        fuel_status=fuel_status if fuel_cost_krw is not None else "unknown",
        toll_status=toll_status,
        toll_mode=toll_mode if toll_krw is not None else "unknown",
        toll_verified=toll_verified if toll_krw is not None else False,
        reason=reason,
    )


def _fuel_status(fuel: FuelCostResult, fuel_cost_krw: int | None) -> str:
    if fuel_cost_krw is None:
        return "unknown"
    if fuel.price is not None and fuel.price.source_status == "fresh":
        return "verified"
    if fuel.price is not None and fuel.price.source_status == "stale":
        return "estimated"
    return "unknown"


def _toll_is_verified(toll: TollResult | None, amount_krw: int | None) -> bool:
    return bool(
        toll is not None
        and toll.complete
        and amount_krw is not None
        and toll.source_status == "fresh"
    )


def _toll_mode(toll: TollResult | None, amount_krw: int | None) -> str:
    if amount_krw is None:
        return "unknown"
    if toll is not None and toll.source_status == "stale":
        return "stale_official"
    return "verified_official"


def _round_trip_toll(
    *,
    outbound_toll: TollResult,
    return_toll: TollResult | None,
    outbound_amount: int | None,
    return_amount: int | None,
    allow_estimate: bool,
    reason: str | None,
) -> tuple[int | None, str, bool, bool, str | None]:
    """Build toll amount and explicit verification metadata for the aggregate."""

    if outbound_amount is not None and return_amount is not None:
        if (
            outbound_toll.source_status == "stale"
            or (return_toll is not None and return_toll.source_status == "stale")
        ):
            return (
                outbound_amount + return_amount,
                "stale_official",
                False,
                False,
                "TOLL_STALE_CACHE",
            )
        return (
            outbound_amount + return_amount,
            "verified_official",
            True,
            False,
            None,
        )
    if allow_estimate and outbound_amount is not None:
        return (
            outbound_amount * 2,
            "estimated_doubled_outbound",
            False,
            True,
            reason or "RETURN_TOLL_UNAVAILABLE",
        )
    return (
        None,
        "unknown",
        False,
        False,
        reason
        or (
            return_toll.reason
            if return_toll is not None and return_toll.reason
            else outbound_toll.reason
            or "TOLL_UNAVAILABLE"
        ),
    )


def _log_cost_breakdown(
    *,
    outbound: DrivingCostLeg,
    return_leg: DrivingCostLeg,
    round_trip: DrivingCostLeg,
    round_trip_toll_mode: str,
) -> None:
    LOGGER.info(
        "driving_cost OUTBOUND route_id=%s distance_m=%s fuel_volume_l=%s "
        "fuel_cost_krw=%s toll_krw=%s total_krw=%s",
        outbound.route_id,
        outbound.distance_m,
        outbound.fuel_volume_l,
        outbound.fuel_cost_krw,
        outbound.toll_krw,
        outbound.total_krw,
    )
    LOGGER.info(
        "driving_cost RETURN route_id=%s distance_m=%s fuel_volume_l=%s "
        "fuel_cost_krw=%s toll_krw=%s toll_mode=%s total_krw=%s",
        return_leg.route_id,
        return_leg.distance_m,
        return_leg.fuel_volume_l,
        return_leg.fuel_cost_krw,
        return_leg.toll_krw,
        return_leg.toll_mode,
        return_leg.total_krw,
    )
    LOGGER.info(
        "driving_cost ROUND_TRIP distance_m=%s fuel_volume_l=%s "
        "fuel_cost_krw=%s toll_krw=%s toll_mode=%s total_krw=%s",
        round_trip.distance_m,
        round_trip.fuel_volume_l,
        round_trip.fuel_cost_krw,
        round_trip.toll_krw,
        round_trip_toll_mode,
        round_trip.total_krw,
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

        async def prefetch_return_toll():
            """Start the reverse toll lookup as soon as reverse OSRM is ready."""

            if reverse_route_task is None:
                return None, None
            try:
                reverse_route = await reverse_route_task
            except Exception as error:  # return route is diagnostic, not a fake cost
                LOGGER.warning(
                    "Return OSRM route unavailable; round trip remains incomplete: %s",
                    error,
                )
                return None, None
            return_route_id = reverse_route.route_id or make_route_id(
                request.destination, request.origin, reverse_route
            )
            return_route = reverse_route.model_copy(update={"route_id": return_route_id})
            return_request = DrivingCostRequest(
                route_id=return_route_id,
                origin=request.destination,
                destination=request.origin,
                route=return_route,
                fuel_type=request.fuel_type,
                fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
                vehicle_class=request.vehicle_class,
                round_trip_mode="doubled_one_way",
            )
            return_toll_response = await self.toll_calculator.calculate(
                self._toll_request(return_request)
            )
            return return_route, return_toll_response

        # This task waits only for reverse OSRM and then immediately starts
        # reverse toll matching.  The outbound toll and fuel-price tasks keep
        # running independently, so a cold return lookup is no longer
        # needlessly postponed behind the outbound source path.
        return_toll_prefetch_task = (
            asyncio.create_task(prefetch_return_toll())
            if reverse_route_task is not None
            else None
        )

        try:
            outbound_toll_response, price = await asyncio.gather(
                outbound_toll_task, price_task
            )
        except BaseException:
            cleanup_tasks = [task for task in (reverse_route_task, return_toll_prefetch_task) if task is not None]
            for task in cleanup_tasks:
                if not task.done():
                    task.cancel()
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            raise
        outbound_toll = self._toll_result(outbound_toll_response)
        reverse_route: RouteResult | None = None
        return_toll_response = None
        if return_toll_prefetch_task is not None:
            try:
                reverse_route, return_toll_response = await return_toll_prefetch_task
            except Exception as error:  # return route is diagnostic, not a fake cost
                LOGGER.warning(
                    "Return toll lookup unavailable; round trip remains incomplete: %s",
                    error,
                )

        outbound_toll_krw = outbound_toll.total_toll_krw if outbound_toll.complete else None
        return_toll: TollResult | None = None
        return_route: RouteResult | None = None
        return_route_id: str | None = None
        return_toll_krw: int | None = None
        return_fuel_cost_krw: int | None = None
        return_fuel_volume_l: float | None = None
        return_distance_m: float | None = None
        round_trip_distance_mode = "unknown"
        round_trip_toll_reason_hint: str | None = None
        reason: str | None = None

        if request.round_trip_mode == "doubled_one_way":
            # This is an explicit user-selected estimate, not a verified
            # reverse route.  FuelCostCalculator uses outbound distance as the
            # estimated return leg and the toll metadata says so explicitly.
            fuel = await self.fuel_service.calculate_with_price(
                request,
                price,
                round_trip_distance_mode="doubled_one_way",
            )
            round_trip_distance_mode = "doubled_one_way"
            round_trip_toll_reason_hint = "RETURN_ROUTE_NOT_REQUESTED_ESTIMATED_DOUBLED_OUTBOUND"
            if fuel.round_trip_complete:
                return_distance_m = request.route.distance_m
                return_fuel_volume_l = fuel.return_fuel_volume_l
                return_fuel_cost_krw = fuel.return_fuel_cost_krw
            reason = "RETURN_ROUTE_NOT_REQUESTED_ESTIMATED_DOUBLED_OUTBOUND"
        elif reverse_route is not None:
            return_route = reverse_route
            return_route_id = reverse_route.route_id
            fuel = await self.fuel_service.calculate_with_price(
                request,
                price,
                round_trip_distance_m=return_route.distance_m,
                round_trip_distance_mode="reverse_route",
            )
            round_trip_distance_mode = "reverse_route"
            round_trip_toll_reason_hint = "RETURN_TOLL_UNAVAILABLE"
            return_distance_m = return_route.distance_m
            if fuel.round_trip_complete:
                return_fuel_volume_l = fuel.return_fuel_volume_l
                return_fuel_cost_krw = fuel.return_fuel_cost_krw
            if return_toll_response is not None:
                return_toll = self._toll_result(return_toll_response)
                return_toll_krw = (
                    return_toll.total_toll_krw if return_toll.complete else None
                )
        else:
            fuel = await self.fuel_service.calculate_outbound_only_with_price(
                request,
                price,
                reason="RETURN_ROUTE_UNAVAILABLE",
            )
            reason = "RETURN_ROUTE_UNAVAILABLE"

        outbound_fuel_cost_krw = fuel.fuel_cost_krw if fuel.complete else None
        outbound_fuel_volume_l = fuel.fuel_volume_l if fuel.complete else None
        fuel_status = _fuel_status(fuel, outbound_fuel_cost_krw)

        can_estimate_return_toll = request.round_trip_mode == "doubled_one_way" or (
            reverse_route is not None
        )
        (
            round_toll_krw,
            round_trip_toll_mode,
            round_trip_toll_verified,
            round_trip_toll_estimated,
            round_trip_toll_reason,
        ) = _round_trip_toll(
            outbound_toll=outbound_toll,
            return_toll=return_toll,
            outbound_amount=outbound_toll_krw,
            return_amount=return_toll_krw,
            allow_estimate=can_estimate_return_toll,
            reason=round_trip_toll_reason_hint,
        )
        if round_trip_toll_estimated and reason is None:
            reason = round_trip_toll_reason
        if not outbound_toll.complete and reason is None:
            reason = outbound_toll.reason or "TOLL_UNAVAILABLE"
        if not fuel.complete and reason is None:
            reason = fuel.reason.value if fuel.reason else "FUEL_PRICE_UNAVAILABLE"

        outbound = _leg(
            route_id=route_id,
            distance_m=request.route.distance_m,
            fuel_volume_l=outbound_fuel_volume_l,
            fuel_cost_krw=outbound_fuel_cost_krw,
            toll_krw=outbound_toll_krw,
            fuel_status=fuel_status,
            toll_mode=_toll_mode(outbound_toll, outbound_toll_krw),
            toll_verified=_toll_is_verified(outbound_toll, outbound_toll_krw),
            reason=None if outbound_toll.complete and fuel.complete else reason,
        )
        return_toll_amount_for_leg = return_toll_krw
        return_toll_mode_for_leg = _toll_mode(return_toll, return_toll_krw)
        return_toll_verified_for_leg = _toll_is_verified(return_toll, return_toll_krw)
        if return_toll_amount_for_leg is None and round_trip_toll_estimated and outbound_toll_krw is not None:
            return_toll_amount_for_leg = outbound_toll_krw
            return_toll_mode_for_leg = "estimated_doubled_outbound"
            return_toll_verified_for_leg = False
        return_leg = _leg(
            route_id=return_route_id,
            distance_m=return_distance_m,
            fuel_volume_l=return_fuel_volume_l,
            fuel_cost_krw=return_fuel_cost_krw,
            toll_krw=return_toll_amount_for_leg,
            fuel_status=fuel_status if return_fuel_cost_krw is not None else "unknown",
            toll_mode=return_toll_mode_for_leg,
            toll_verified=return_toll_verified_for_leg,
            reason=round_trip_toll_reason if return_toll_amount_for_leg is not None and not return_toll_verified_for_leg else reason,
        )

        round_trip_has_all_fuel = (
            outbound_fuel_cost_krw is not None
            and return_fuel_cost_krw is not None
            and outbound_fuel_volume_l is not None
            and return_fuel_volume_l is not None
            and return_distance_m is not None
        )
        round_trip = _leg(
            route_id=None,
            distance_m=(request.route.distance_m + return_distance_m) if round_trip_has_all_fuel else None,
            fuel_volume_l=(outbound_fuel_volume_l + return_fuel_volume_l) if round_trip_has_all_fuel else None,
            fuel_cost_krw=(outbound_fuel_cost_krw + return_fuel_cost_krw) if round_trip_has_all_fuel else None,
            toll_krw=round_toll_krw,
            fuel_status=fuel_status if round_trip_has_all_fuel else "unknown",
            toll_mode=round_trip_toll_mode,
            toll_verified=round_trip_toll_verified,
            reason=reason,
        )
        round_trip_toll = RoundTripToll(
            amount_krw=round_toll_krw,
            mode=round_trip_toll_mode,
            verified=round_trip_toll_verified,
            estimated=round_trip_toll_estimated,
            complete=round_trip_toll_verified,
            reason=round_trip_toll_reason,
        )
        cost_complete = outbound.complete and return_leg.complete and round_trip.complete
        officially_verified = (
            cost_complete
            and outbound.status == "verified"
            and return_leg.status == "verified"
            and round_trip.status == "verified"
            and round_trip_toll.verified
        )
        contains_estimate = any(
            leg.status == "estimated" for leg in (outbound, return_leg, round_trip)
        ) or round_trip_toll.estimated
        result = DrivingCostResult(
            complete=cost_complete,
            cost_complete=cost_complete,
            outbound=outbound,
            return_leg=return_leg,
            round_trip=round_trip,
            round_trip_toll=round_trip_toll,
            officially_verified=officially_verified,
            contains_estimate=contains_estimate,
            round_trip_distance_mode=round_trip_distance_mode,
            reason=reason,
        )
        _log_cost_breakdown(
            outbound=outbound,
            return_leg=return_leg,
            round_trip=round_trip,
            round_trip_toll_mode=round_trip_toll.mode,
        )
        return DrivingCostResponse(
            status="ok" if result.cost_complete else "partial",
            route=request.route,
            outbound_route=request.route,
            return_route=return_route,
            toll=outbound_toll,
            outbound_toll=outbound_toll,
            return_toll=return_toll,
            fuel=fuel,
            driving_cost=result,
        )


def make_fuel_response(result: FuelCostResult) -> FuelResponse:
    return FuelResponse(status="ok" if result.complete else "partial", fuel=result)
