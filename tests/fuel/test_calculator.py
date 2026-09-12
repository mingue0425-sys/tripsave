import math

import pytest

from backend.fuel.calculator import FuelCalculationError, FuelCostCalculator
from backend.fuel.models import FuelType

from .helpers import make_price


def test_calculator_uses_route_metres_and_rounds_only_final_krw() -> None:
    result = FuelCostCalculator.calculate(
        distance_m=100_000.0,
        fuel_efficiency_km_per_l=10.0,
        price=make_price(price=1_700.0),
    )

    assert result.distance_km == 100.0
    assert result.fuel_volume_l == 10.0
    assert result.one_way_krw == 17_000
    assert result.round_trip_distance_km == 200.0
    assert result.round_trip_krw == 34_000
    assert result.round_trip_distance_mode == "doubled_one_way"


@pytest.mark.parametrize("fuel_type", list(FuelType))
def test_calculator_supports_all_internal_fuel_types(fuel_type: FuelType) -> None:
    result = FuelCostCalculator.calculate(
        distance_m=50_000.0,
        fuel_efficiency_km_per_l=12.5,
        price=make_price(fuel_type, price=1_600.25),
    )
    assert result.complete is True
    assert result.fuel_type is fuel_type
    assert result.one_way_krw == 6_401


def test_calculator_supports_directional_return_distance() -> None:
    result = FuelCostCalculator.calculate(
        distance_m=100_000.0,
        round_trip_distance_m=120_000.0,
        round_trip_distance_mode="reverse_route",
        fuel_efficiency_km_per_l=10.0,
        price=make_price(price=1_700.0),
    )
    assert result.round_trip_distance_km == 220.0
    assert result.round_trip_distance_km == result.distance_km + result.return_distance_km
    assert result.round_trip_krw == 37_400
    assert result.round_trip_krw == result.one_way_krw + result.return_fuel_cost_krw
    assert result.round_trip_distance_mode == "reverse_route"


@pytest.mark.parametrize(
    "efficiency",
    [0, -1, 0.099, 100.001, math.nan, math.inf, -math.inf, "13.5"],
)
def test_calculator_rejects_invalid_efficiency(efficiency: object) -> None:
    with pytest.raises(FuelCalculationError):
        FuelCostCalculator.calculate(
            distance_m=100_000.0,
            fuel_efficiency_km_per_l=efficiency,  # type: ignore[arg-type]
            price=make_price(),
        )


@pytest.mark.parametrize("distance", [0, -1, math.nan, math.inf, "100000"])
def test_calculator_rejects_invalid_distance(distance: object) -> None:
    with pytest.raises(FuelCalculationError):
        FuelCostCalculator.calculate(
            distance_m=distance,  # type: ignore[arg-type]
            fuel_efficiency_km_per_l=10.0,
            price=make_price(),
        )
