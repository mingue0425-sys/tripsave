"""Pure fuel-cost arithmetic kept independent from web crawling."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

from backend.fuel.models import FuelCostResult, FuelPriceResult


class FuelCalculationError(ValueError):
    """Invalid arithmetic input; callers must not turn it into a zero cost."""


def round_krw(value: float) -> int:
    """Round non-negative KRW values only at the final currency boundary."""

    if not math.isfinite(value) or value < 0:
        raise FuelCalculationError("KRW value must be a finite non-negative number.")
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class FuelCostCalculator:
    """Calculate one-way and return fuel costs from a verified price."""

    @staticmethod
    def calculate(
        *,
        distance_m: float,
        fuel_efficiency_km_per_l: float,
        price: FuelPriceResult,
        round_trip_distance_m: float | None = None,
        round_trip_distance_mode: str = "doubled_one_way",
    ) -> FuelCostResult:
        if (
            isinstance(distance_m, bool)
            or not isinstance(distance_m, (int, float))
            or not math.isfinite(float(distance_m))
            or float(distance_m) <= 0
        ):
            raise FuelCalculationError("Route distance must be a positive finite number of metres.")
        if (
            isinstance(fuel_efficiency_km_per_l, bool)
            or not isinstance(fuel_efficiency_km_per_l, (int, float))
            or not math.isfinite(float(fuel_efficiency_km_per_l))
            or not 0.1 <= float(fuel_efficiency_km_per_l) <= 100.0
        ):
            raise FuelCalculationError("Fuel efficiency must be within 0.1 to 100 km/L.")
        if not price.complete or price.price_krw_per_l is None:
            raise FuelCalculationError("A complete fuel price is required for cost calculation.")
        if round_trip_distance_mode not in {"doubled_one_way", "reverse_route"}:
            raise FuelCalculationError("Unknown round-trip distance mode.")
        outbound_km = float(distance_m) / 1000.0
        return_km = (
            outbound_km * 2.0
            if round_trip_distance_m is None
            else float(round_trip_distance_m) / 1000.0
        )
        if not math.isfinite(return_km) or return_km <= 0:
            raise FuelCalculationError("Return route distance must be positive and finite.")
        efficiency = float(fuel_efficiency_km_per_l)
        unit_price = float(price.price_krw_per_l)
        outbound_volume = outbound_km / efficiency
        return_volume = return_km / efficiency
        outbound_cost = outbound_volume * unit_price
        return_cost = return_volume * unit_price
        one_way_krw = round_krw(outbound_cost)
        round_trip_krw = round_krw(return_cost)
        confidence = "stale" if price.source_status == "stale" else "estimated"
        return FuelCostResult(
            complete=True,
            fuel_type=price.fuel_type,
            fuel_efficiency_km_per_l=efficiency,
            price=price,
            price_krw_per_l=unit_price,
            distance_km=outbound_km,
            fuel_volume_l=outbound_volume,
            fuel_cost_krw=one_way_krw,
            one_way_krw=one_way_krw,
            round_trip_distance_km=return_km,
            round_trip_fuel_volume_l=return_volume,
            round_trip_fuel_cost_krw=round_trip_krw,
            round_trip_krw=round_trip_krw,
            round_trip_distance_mode=round_trip_distance_mode,
            confidence=confidence,
        )
