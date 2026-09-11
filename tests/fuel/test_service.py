import asyncio

from backend.fuel.models import (
    DrivingCostRequest,
    FuelCalculationRequest,
    FuelFailureCode,
    FuelPriceResult,
    FuelType,
)
from backend.fuel.service import DrivingCostService, FuelPriceService
from backend.tolls.models import TollResponse, TollVehicleClass

from .helpers import make_price, make_route, make_toll


def fuel_request(*, fuel_type: FuelType = FuelType.GASOLINE) -> FuelCalculationRequest:
    origin, destination, route = make_route(distance_m=100_000.0)
    return FuelCalculationRequest(
        route_id=route.route_id,
        origin=origin,
        destination=destination,
        route=route,
        fuel_type=fuel_type,
        fuel_efficiency_km_per_l=10.0,
    )


class StubPriceSource:
    def __init__(self, result: FuelPriceResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    async def get_price(self, fuel_type: FuelType) -> FuelPriceResult:
        self.calls += 1
        if self.error:
            raise self.error
        assert self.result is not None
        return self.result.model_copy(update={"fuel_type": fuel_type})


def test_fuel_service_uses_verified_price_and_never_zero_on_source_failure(tmp_path) -> None:
    source = StubPriceSource(make_price())
    service = FuelPriceService(database_path=tmp_path / "fuel.db", source=source)

    result = asyncio.run(service.calculate(fuel_request()))
    assert result.complete is True
    assert result.fuel_volume_l == 10.0
    assert result.one_way_krw == 17_000

    failing = StubPriceSource(error=RuntimeError("not a source error"))
    # The production source contract raises FuelSourceError.  This test uses
    # the real exception type to verify the explicit unavailable state.
    from backend.fuel.official import FuelSourceError

    failing.error = FuelSourceError("official page unavailable")
    unavailable_service = FuelPriceService(
        database_path=tmp_path / "unavailable.db", source=failing
    )
    unavailable = asyncio.run(unavailable_service.calculate(fuel_request()))
    assert unavailable.complete is False
    assert unavailable.one_way_krw is None
    assert unavailable.reason is FuelFailureCode.FUEL_SOURCE_FAILED


def test_fuel_service_cache_avoids_second_source_request(tmp_path) -> None:
    source = StubPriceSource(make_price(FuelType.DIESEL, price=1_844.02))
    service = FuelPriceService(database_path=tmp_path / "fuel.db", source=source)

    first = asyncio.run(service.get_price(FuelType.DIESEL))
    second = asyncio.run(service.get_price(FuelType.DIESEL))

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert source.calls == 1


class StubTollService:
    def __init__(self, outbound_total: int | None = 17_500, return_total: int | None = 18_000):
        self.outbound_total = outbound_total
        self.return_total = return_total
        self.calls = 0

    async def calculate(self, request):
        self.calls += 1
        total = self.outbound_total if self.calls % 2 else self.return_total
        toll = make_toll(total=total, route_id=request.route_id or "route-test")
        return TollResponse(status="ok" if toll.complete else "partial", toll=toll)


class StubRouting:
    async def route(self, origin, destination):
        _, _, route = make_route(distance_m=120_000.0, origin=origin, destination=destination)
        return route


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


def test_driving_cost_sums_directional_official_tolls_and_reverse_fuel(tmp_path) -> None:
    price_service = FuelPriceService(
        database_path=tmp_path / "fuel.db",
        source=StubPriceSource(make_price()),
    )
    service = DrivingCostService(
        toll_calculator=StubTollService(),
        fuel_service=price_service,
        routing_client=StubRouting(),
    )

    result = asyncio.run(service.calculate(driving_request()))

    assert result.status == "ok"
    assert result.driving_cost.complete is True
    assert result.driving_cost.one_way.total_krw == 34_500
    assert result.driving_cost.round_trip.fuel_krw == 20_400
    assert result.driving_cost.round_trip.toll_krw == 35_500
    assert result.driving_cost.round_trip.total_krw == 55_900
    assert result.driving_cost.round_trip_distance_mode == "reverse_route"
    assert result.driving_cost.round_trip_toll_mode == "directional_official"
    assert result.return_toll is not None and result.return_toll.total_toll_krw == 18_000


def test_driving_cost_doubles_only_known_outbound_toll_when_return_route_unavailable(tmp_path) -> None:
    class BrokenRouting:
        async def route(self, origin, destination):
            raise RuntimeError("OSRM return route failed")

    price_service = FuelPriceService(
        database_path=tmp_path / "fuel.db",
        source=StubPriceSource(make_price()),
    )
    service = DrivingCostService(
        toll_calculator=StubTollService(outbound_total=17_500),
        fuel_service=price_service,
        routing_client=BrokenRouting(),
    )

    result = asyncio.run(service.calculate(driving_request()))

    assert result.status == "ok"
    assert result.driving_cost.round_trip_toll_mode == "doubled_one_way"
    assert result.driving_cost.round_trip.toll_krw == 35_000
    assert result.driving_cost.round_trip.total_krw == 69_000
    assert result.driving_cost.reason == "RETURN_ROUTE_UNAVAILABLE_USED_DOUBLED_ONE_WAY"


def test_directional_return_toll_failure_keeps_outbound_official_value_and_marks_fallback(tmp_path) -> None:
    price_service = FuelPriceService(
        database_path=tmp_path / "fuel.db",
        source=StubPriceSource(make_price()),
    )
    service = DrivingCostService(
        toll_calculator=StubTollService(outbound_total=17_500, return_total=None),
        fuel_service=price_service,
        routing_client=StubRouting(),
    )

    result = asyncio.run(service.calculate(driving_request()))

    assert result.status == "ok"
    assert result.return_toll is not None and result.return_toll.complete is False
    assert result.toll.complete is True and result.toll.total_toll_krw == 17_500
    assert result.driving_cost.round_trip_toll_mode == "doubled_one_way"
    assert result.driving_cost.round_trip.toll_krw == 35_000
    assert result.driving_cost.reason == "RETURN_TOLL_UNAVAILABLE_USED_DOUBLED_ONE_WAY"
