"""V0.5.1 round-trip cost integrity tests."""

import asyncio

import pytest

from backend.fuel.calculator import FuelCostCalculator
from backend.fuel.models import (
    DrivingCostLeg,
    DrivingCostRequest,
    DrivingCostResult,
    FuelType,
    RoundTripToll,
)
from backend.fuel.service import DrivingCostService
from backend.tolls.models import TollResponse, TollVehicleClass

from .helpers import make_price, make_route, make_toll


class FixedFuelService:
    async def get_price(self, fuel_type: FuelType):
        return make_price(fuel_type)

    async def calculate_with_price(self, request, price, *, round_trip_distance_m=None, round_trip_distance_mode="doubled_one_way"):
        return FuelCostCalculator.calculate(
            distance_m=request.route.distance_m,
            round_trip_distance_m=round_trip_distance_m,
            round_trip_distance_mode=round_trip_distance_mode,
            fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
            price=price,
        )

    async def calculate_outbound_only_with_price(self, request, price, *, reason="RETURN_ROUTE_UNAVAILABLE"):
        return FuelCostCalculator.calculate_outbound_only(
            distance_m=request.route.distance_m,
            fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
            price=price,
            reason=reason,
        )


class FixedTollService:
    def __init__(self, outbound_total=5_000, return_total=6_000):
        self.outbound_total = outbound_total
        self.return_total = return_total
        self.calls = 0

    async def calculate(self, request):
        self.calls += 1
        total = self.outbound_total if self.calls % 2 else self.return_total
        toll = make_toll(total=total, route_id=request.route_id or "route-test")
        return TollResponse(status="ok" if toll.complete else "partial", toll=toll)


class FixedRouting:
    def __init__(self, return_distance_m=120_000.0):
        self.return_distance_m = return_distance_m

    async def route(self, origin, destination):
        _, _, route = make_route(
            distance_m=self.return_distance_m,
            origin=origin,
            destination=destination,
        )
        return route


class BrokenRouting:
    async def route(self, origin, destination):
        raise RuntimeError("return route unavailable")


def driving_request() -> DrivingCostRequest:
    origin, destination, route = make_route(distance_m=100_000.0)
    return DrivingCostRequest(
        route_id=route.route_id,
        origin=origin,
        destination=destination,
        route=route,
        fuel_type=FuelType.GASOLINE,
        fuel_efficiency_km_per_l=10.0,
        vehicle_class=TollVehicleClass.CLASS_1,
        round_trip_mode="directional",
    )


def test_deterministic_symmetric_round_trip_arithmetic() -> None:
    result = FuelCostCalculator.calculate(
        distance_m=100_000.0,
        fuel_efficiency_km_per_l=10.0,
        price=make_price(price=1_700.0),
    )

    assert result.distance_km == 100.0
    assert result.return_distance_km == 100.0
    assert result.round_trip_distance_km == 200.0
    assert result.fuel_volume_l == 10.0
    assert result.return_fuel_volume_l == 10.0
    assert result.fuel_cost_krw == 17_000
    assert result.return_fuel_cost_krw == 17_000
    assert result.round_trip_fuel_cost_krw == 34_000
    assert result.round_trip_krw == 34_000


def test_asymmetric_round_trip_uses_each_route_distance() -> None:
    result = FuelCostCalculator.calculate(
        distance_m=100_000.0,
        round_trip_distance_m=120_000.0,
        round_trip_distance_mode="reverse_route",
        fuel_efficiency_km_per_l=10.0,
        price=make_price(price=1_700.0),
    )

    assert result.return_distance_km == 120.0
    assert result.round_trip_distance_km == 220.0
    assert result.fuel_cost_krw == 17_000
    assert result.return_fuel_cost_krw == 20_400
    assert result.round_trip_fuel_cost_krw == 37_400


def test_deterministic_symmetric_service_totals_match_requested_example() -> None:
    service = DrivingCostService(
        toll_calculator=FixedTollService(outbound_total=5_000, return_total=6_000),
        fuel_service=FixedFuelService(),
        routing_client=FixedRouting(return_distance_m=100_000.0),
    )

    result = asyncio.run(service.calculate(driving_request()))
    outbound = result.driving_cost.outbound
    return_leg = result.driving_cost.return_leg
    round_trip = result.driving_cost.round_trip

    assert outbound.fuel_volume_l == 10.0
    assert return_leg.fuel_volume_l == 10.0
    assert outbound.fuel_cost_krw == 17_000
    assert return_leg.fuel_cost_krw == 17_000
    assert outbound.toll_krw == 5_000
    assert return_leg.toll_krw == 6_000
    assert outbound.total_krw == 22_000
    assert return_leg.total_krw == 23_000
    assert round_trip.fuel_cost_krw == 34_000
    assert round_trip.toll_krw == 11_000
    assert round_trip.total_krw == 45_000


def test_directional_service_sums_canonical_legs() -> None:
    service = DrivingCostService(
        toll_calculator=FixedTollService(),
        fuel_service=FixedFuelService(),
        routing_client=FixedRouting(),
    )

    result = asyncio.run(service.calculate(driving_request()))
    outbound = result.driving_cost.outbound
    return_leg = result.driving_cost.return_leg
    round_trip = result.driving_cost.round_trip

    assert result.status == "ok"
    assert result.driving_cost.cost_complete is True
    assert outbound.distance_m == 100_000.0
    assert return_leg.distance_m == 120_000.0
    assert outbound.fuel_cost_krw == 17_000
    assert return_leg.fuel_cost_krw == 20_400
    assert outbound.toll_krw == 5_000
    assert return_leg.toll_krw == 6_000
    assert outbound.total_krw == 22_000
    assert return_leg.total_krw == 26_400
    assert round_trip.distance_m == 220_000.0
    assert round_trip.fuel_cost_krw == 37_400
    assert round_trip.toll_krw == 11_000
    assert round_trip.total_krw == 48_400
    assert round_trip.total_krw == outbound.total_krw + return_leg.total_krw
    assert result.driving_cost.round_trip_toll.mode == "verified_official"
    assert result.driving_cost.round_trip_toll.verified is True
    assert result.driving_cost.officially_verified is True


def test_return_toll_estimate_is_not_official_but_cost_arithmetic_remains_explicit() -> None:
    service = DrivingCostService(
        toll_calculator=FixedTollService(return_total=None),
        fuel_service=FixedFuelService(),
        routing_client=FixedRouting(),
    )

    result = asyncio.run(service.calculate(driving_request()))
    driving = result.driving_cost

    assert result.status == "ok"
    assert driving.cost_complete is True
    assert driving.officially_verified is False
    assert driving.contains_estimate is True
    assert driving.round_trip_toll == RoundTripToll(
        amount_krw=10_000,
        mode="estimated_doubled_outbound",
        verified=False,
        estimated=True,
        complete=False,
        reason="RETURN_TOLL_UNAVAILABLE",
    )
    assert driving.return_leg.toll_krw == 5_000
    assert driving.return_leg.toll_verified is False
    assert driving.return_leg.total_krw == 25_400
    assert driving.round_trip.total_krw == 47_400
    assert driving.round_trip.total_krw == driving.outbound.total_krw + driving.return_leg.total_krw


def test_reverse_route_failure_does_not_fabricate_a_complete_round_trip() -> None:
    service = DrivingCostService(
        toll_calculator=FixedTollService(),
        fuel_service=FixedFuelService(),
        routing_client=BrokenRouting(),
    )

    result = asyncio.run(service.calculate(driving_request()))
    driving = result.driving_cost

    assert result.status == "partial"
    assert driving.cost_complete is False
    assert driving.round_trip.complete is False
    assert driving.round_trip.total_krw is None
    assert driving.return_leg.distance_m is None
    assert driving.return_leg.fuel_cost_krw is None
    assert driving.round_trip_toll.amount_krw is None
    assert driving.round_trip_toll.mode == "unknown"
    assert driving.round_trip_toll.verified is False
    assert driving.reason == "RETURN_ROUTE_UNAVAILABLE"


def test_explicit_doubled_mode_is_an_estimate_with_separate_toll_metadata() -> None:
    request = driving_request().model_copy(update={"round_trip_mode": "doubled_one_way"})
    service = DrivingCostService(
        toll_calculator=FixedTollService(return_total=None),
        fuel_service=FixedFuelService(),
        routing_client=BrokenRouting(),
    )

    result = asyncio.run(service.calculate(request))
    driving = result.driving_cost

    assert result.status == "ok"
    assert driving.cost_complete is True
    assert driving.officially_verified is False
    assert driving.contains_estimate is True
    assert result.return_route is None
    assert driving.round_trip_toll == RoundTripToll(
        amount_krw=10_000,
        mode="estimated_doubled_outbound",
        verified=False,
        estimated=True,
        complete=False,
        reason="RETURN_ROUTE_NOT_REQUESTED_ESTIMATED_DOUBLED_OUTBOUND",
    )
    assert driving.round_trip.total_krw == 44_000
    assert driving.round_trip.total_krw == (
        driving.outbound.total_krw + driving.return_leg.total_krw
    )


def test_schema_rejects_a_round_trip_that_omits_outbound_fuel() -> None:
    outbound = DrivingCostLeg(
        complete=True,
        route_id="outbound",
        distance_m=100_000.0,
        fuel_volume_l=10.0,
        fuel_cost_krw=17_000,
        toll_krw=5_000,
        total_krw=22_000,
        status="verified",
        fuel_status="verified",
        toll_status="verified",
        toll_mode="verified_official",
        toll_verified=True,
    )
    return_leg = outbound.model_copy(
        update={
            "route_id": "return",
            "distance_m": 120_000.0,
            "fuel_volume_l": 12.0,
            "fuel_cost_krw": 20_400,
            "toll_krw": 6_000,
            "total_krw": 26_400,
        }
    )
    bad_round_trip = outbound.model_copy(
        update={
            "route_id": None,
            "distance_m": 220_000.0,
            "fuel_volume_l": 22.0,
            "fuel_cost_krw": 20_400,
            "toll_krw": 11_000,
            "total_krw": 31_400,
        }
    )

    with pytest.raises(ValueError, match="round-trip fuel cost"):
        DrivingCostResult(
            complete=True,
            cost_complete=True,
            outbound=outbound,
            return_leg=return_leg,
            round_trip=bad_round_trip,
            round_trip_toll=RoundTripToll(
                amount_krw=11_000,
                mode="verified_official",
                verified=True,
                complete=True,
            ),
            officially_verified=True,
            contains_estimate=False,
            round_trip_distance_mode="reverse_route",
        )
