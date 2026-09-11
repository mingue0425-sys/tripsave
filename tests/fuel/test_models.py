import math

import pytest
from pydantic import ValidationError

from backend.fuel.models import FuelCalculationRequest, FuelPriceResult, FuelType

from .helpers import make_route


@pytest.mark.parametrize("efficiency", [0, -1, math.nan, math.inf, "13.5"])
def test_request_rejects_invalid_efficiency(efficiency: object) -> None:
    origin, destination, route = make_route()
    with pytest.raises(ValidationError):
        FuelCalculationRequest(
            origin=origin,
            destination=destination,
            route=route,
            fuel_type=FuelType.GASOLINE,
            fuel_efficiency_km_per_l=efficiency,  # type: ignore[arg-type]
        )


def test_incomplete_price_does_not_allow_zero_as_unavailable_value() -> None:
    with pytest.raises(ValidationError):
        FuelPriceResult(
            fuel_type=FuelType.GASOLINE,
            price_krw_per_l=0,
            source_url="https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
            complete=False,
        )
