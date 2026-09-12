from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import app, driving_cost_service, fuel_price_service
from backend.fuel.models import (
    DrivingCostLeg,
    DrivingCostResponse,
    DrivingCostResult,
    FuelCostResult,
    FuelType,
    RoundTripToll,
)

from .helpers import make_route, make_toll


client = TestClient(app)


def payload() -> dict[str, object]:
    origin, destination, route = make_route(distance_m=100_000.0)
    return {
        "route_id": route.route_id,
        "origin": origin.model_dump(),
        "destination": destination.model_dump(),
        "route": route.model_dump(),
        "fuel_type": "gasoline",
        "fuel_efficiency_km_per_l": 10.0,
    }


def test_fuel_status_exposes_public_source_policy_and_cache_ttl() -> None:
    response = client.get("/api/fuel/status")

    assert response.status_code == 200
    body = response.json()
    assert body["supported_fuel_types"] == ["gasoline", "diesel", "lpg"]
    assert body["cache_ttl_s"] == 10_800.0
    assert body["source_urls"]["lpg"].endswith("dopVsAvselSelect.do")


def test_fuel_api_rejects_zero_nan_and_infinite_efficiency() -> None:
    for efficiency in [0, -1, "NaN", "Infinity"]:
        request = payload()
        request["fuel_efficiency_km_per_l"] = efficiency
        response = client.post("/api/fuel/calculate", json=request)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_fuel_api_keeps_unavailable_price_explicit(monkeypatch) -> None:
    async def unavailable(request):
        return FuelCostResult(
            complete=False,
            fuel_type=request.fuel_type,
            fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
            distance_km=request.route.distance_m / 1000,
            reason="FUEL_PRICE_UNAVAILABLE",
        )

    monkeypatch.setattr(fuel_price_service, "calculate", unavailable)
    response = client.post("/api/fuel/calculate", json=payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["fuel"]["complete"] is False
    assert "one_way_krw" not in body["fuel"]
    assert body["fuel"].get("price_krw_per_l") is None


def test_driving_cost_api_exposes_canonical_aggregate_shape(monkeypatch) -> None:
    async def aggregate(request):
        origin, destination, route = make_route(distance_m=100_000.0)
        from .helpers import make_price
        from backend.fuel.calculator import FuelCostCalculator
        fuel = FuelCostCalculator.calculate(
            distance_m=route.distance_m,
            fuel_efficiency_km_per_l=request.fuel_efficiency_km_per_l,
            price=make_price(),
        )
        outbound_toll = make_toll(total=17_500)
        return_toll = make_toll(total=17_500)
        outbound = DrivingCostLeg(
            complete=True,
            route_id=route.route_id or "route-test",
            distance_m=route.distance_m,
            fuel_volume_l=fuel.fuel_volume_l,
            fuel_cost_krw=fuel.fuel_cost_krw,
            toll_krw=outbound_toll.total_toll_krw,
            total_krw=fuel.fuel_cost_krw + outbound_toll.total_toll_krw,
            status="verified",
            fuel_status="verified",
            toll_status="verified",
            toll_mode="verified_official",
            toll_verified=True,
        )
        return_leg = DrivingCostLeg(
            complete=True,
            route_id="return-route",
            distance_m=fuel.return_distance_km * 1000,
            fuel_volume_l=fuel.return_fuel_volume_l,
            fuel_cost_krw=fuel.return_fuel_cost_krw,
            toll_krw=return_toll.total_toll_krw,
            total_krw=fuel.return_fuel_cost_krw + return_toll.total_toll_krw,
            status="verified",
            fuel_status="verified",
            toll_status="verified",
            toll_mode="verified_official",
            toll_verified=True,
        )
        round_trip = DrivingCostLeg(
            complete=True,
            distance_m=route.distance_m + fuel.return_distance_km * 1000,
            fuel_volume_l=fuel.fuel_volume_l + fuel.return_fuel_volume_l,
            fuel_cost_krw=fuel.round_trip_fuel_cost_krw,
            toll_krw=35_000,
            total_krw=fuel.round_trip_fuel_cost_krw + 35_000,
            status="verified",
            fuel_status="verified",
            toll_status="verified",
            toll_mode="verified_official",
            toll_verified=True,
        )
        return DrivingCostResponse(
            status="ok",
            route=route,
            toll=outbound_toll,
            outbound_toll=outbound_toll,
            return_toll=return_toll,
            fuel=fuel,
            driving_cost=DrivingCostResult(
                complete=True,
                cost_complete=True,
                outbound=outbound,
                return_leg=return_leg,
                round_trip=round_trip,
                round_trip_toll=RoundTripToll(
                    amount_krw=35_000,
                    mode="verified_official",
                    verified=True,
                    complete=True,
                ),
                officially_verified=True,
                contains_estimate=False,
                round_trip_distance_mode="doubled_one_way",
            ),
        )

    monkeypatch.setattr(driving_cost_service, "calculate", aggregate)
    response = client.post("/api/costs/driving", json=payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["fuel"]["fuel_type"] == FuelType.GASOLINE.value
    assert body["toll"]["total_toll_krw"] == 17_500
    assert body["driving_cost"]["outbound"]["total_krw"] == 34_500
    assert body["driving_cost"]["return"]["total_krw"] == 34_500
    assert body["driving_cost"]["round_trip"]["fuel_cost_krw"] == 34_000
    assert body["driving_cost"]["round_trip"]["total_krw"] == 69_000
    assert body["driving_cost"]["round_trip"]["total_krw"] == (
        body["driving_cost"]["outbound"]["total_krw"]
        + body["driving_cost"]["return"]["total_krw"]
    )
    assert "one_way" not in body["driving_cost"]
    assert "return_cost" not in body["driving_cost"]
