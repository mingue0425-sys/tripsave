"""Pure fuel-cost arithmetic kept independent from web crawling."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from backend.fuel.models import FuelCostResult, FuelPriceResult


class FuelCalculationError(ValueError):
    """Invalid arithmetic input; callers must not turn it into a zero cost."""


def round_krw(value: float | Decimal) -> int:
    """Round non-negative KRW values only at the final currency boundary."""

    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError):
        raise FuelCalculationError("KRW value must be a finite non-negative number.") from None
    if not decimal_value.is_finite() or decimal_value < 0:
        raise FuelCalculationError("KRW value must be a finite non-negative number.")
    return int(decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class FuelLegCost:
    """Raw route distance plus the independently rounded cost for one leg."""

    distance_km: float
    fuel_volume_l: float
    fuel_cost_krw: int


class FuelCostCalculator:
    """Calculate outbound, return, and aggregate fuel costs.

    ``round_trip_distance_m`` is the *return-leg* distance when supplied.  The
    aggregate distance and cost are then the sum of the two legs; it is never
    the return-leg value by itself.  When no return route is supplied, the
    public fuel endpoint uses an explicit symmetric estimate (the outbound
    distance is used for the estimated return leg).
    """

    @staticmethod
    def calculate_leg(
        *,
        distance_m: float,
        fuel_efficiency_km_per_l: float,
        price: FuelPriceResult,
    ) -> FuelLegCost:
        """Calculate one leg, keeping Decimal precision until KRW rounding."""

        FuelCostCalculator._validate_distance(distance_m)
        FuelCostCalculator._validate_efficiency(fuel_efficiency_km_per_l)
        if not price.complete or price.price_krw_per_l is None:
            raise FuelCalculationError("A complete fuel price is required for cost calculation.")

        distance_km_decimal = Decimal(str(distance_m)) / Decimal("1000")
        efficiency_decimal = Decimal(str(fuel_efficiency_km_per_l))
        price_decimal = Decimal(str(price.price_krw_per_l))
        volume_decimal = distance_km_decimal / efficiency_decimal
        raw_cost = volume_decimal * price_decimal
        return FuelLegCost(
            distance_km=float(distance_km_decimal),
            fuel_volume_l=float(volume_decimal),
            fuel_cost_krw=round_krw(raw_cost),
        )

    @staticmethod
    def _validate_distance(distance_m: object) -> None:
        if (
            isinstance(distance_m, bool)
            or not isinstance(distance_m, (int, float))
            or not math.isfinite(float(distance_m))
            or float(distance_m) <= 0
        ):
            raise FuelCalculationError("Route distance must be a positive finite number of metres.")

    @staticmethod
    def _validate_efficiency(fuel_efficiency_km_per_l: object) -> None:
        if (
            isinstance(fuel_efficiency_km_per_l, bool)
            or not isinstance(fuel_efficiency_km_per_l, (int, float))
            or not math.isfinite(float(fuel_efficiency_km_per_l))
            or not 0.1 <= float(fuel_efficiency_km_per_l) <= 100.0
        ):
            raise FuelCalculationError("Fuel efficiency must be within 0.1 to 100 km/L.")

    @staticmethod
    def calculate(
        *,
        distance_m: float,
        fuel_efficiency_km_per_l: float,
        price: FuelPriceResult,
        round_trip_distance_m: float | None = None,
        round_trip_distance_mode: str = "doubled_one_way",
    ) -> FuelCostResult:
        FuelCostCalculator._validate_distance(distance_m)
        FuelCostCalculator._validate_efficiency(fuel_efficiency_km_per_l)
        if round_trip_distance_mode not in {"doubled_one_way", "reverse_route"}:
            raise FuelCalculationError("Unknown round-trip distance mode.")

        outbound = FuelCostCalculator.calculate_leg(
            distance_m=distance_m,
            fuel_efficiency_km_per_l=fuel_efficiency_km_per_l,
            price=price,
        )
        return_distance_m = distance_m if round_trip_distance_m is None else round_trip_distance_m
        FuelCostCalculator._validate_distance(return_distance_m)
        return_leg = FuelCostCalculator.calculate_leg(
            distance_m=return_distance_m,
            fuel_efficiency_km_per_l=fuel_efficiency_km_per_l,
            price=price,
        )

        round_trip_distance_km = outbound.distance_km + return_leg.distance_km
        round_trip_fuel_volume_l = outbound.fuel_volume_l + return_leg.fuel_volume_l
        # Each leg is a currency boundary.  Aggregating the rounded leg values
        # keeps the displayed round-trip amount exactly equal to displayed
        # outbound plus displayed return, including half-up edge cases.
        round_trip_fuel_cost_krw = outbound.fuel_cost_krw + return_leg.fuel_cost_krw
        efficiency = float(fuel_efficiency_km_per_l)
        unit_price = float(price.price_krw_per_l) if price.price_krw_per_l is not None else None
        confidence = "stale" if price.source_status == "stale" else "estimated"
        return FuelCostResult(
            complete=True,
            fuel_type=price.fuel_type,
            fuel_efficiency_km_per_l=efficiency,
            price=price,
            price_krw_per_l=unit_price,
            distance_km=outbound.distance_km,
            fuel_volume_l=outbound.fuel_volume_l,
            fuel_cost_krw=outbound.fuel_cost_krw,
            one_way_krw=outbound.fuel_cost_krw,
            return_distance_km=return_leg.distance_km,
            return_fuel_volume_l=return_leg.fuel_volume_l,
            return_fuel_cost_krw=return_leg.fuel_cost_krw,
            round_trip_complete=True,
            round_trip_distance_km=round_trip_distance_km,
            round_trip_fuel_volume_l=round_trip_fuel_volume_l,
            round_trip_fuel_cost_krw=round_trip_fuel_cost_krw,
            round_trip_krw=round_trip_fuel_cost_krw,
            round_trip_distance_mode=round_trip_distance_mode,
            confidence=confidence,
        )

    @staticmethod
    def calculate_outbound_only(
        *,
        distance_m: float,
        fuel_efficiency_km_per_l: float,
        price: FuelPriceResult,
        reason: str = "RETURN_ROUTE_UNAVAILABLE",
    ) -> FuelCostResult:
        """Calculate only the known outbound leg when return routing failed."""

        outbound = FuelCostCalculator.calculate_leg(
            distance_m=distance_m,
            fuel_efficiency_km_per_l=fuel_efficiency_km_per_l,
            price=price,
        )
        return FuelCostResult(
            complete=True,
            fuel_type=price.fuel_type,
            fuel_efficiency_km_per_l=float(fuel_efficiency_km_per_l),
            price=price,
            price_krw_per_l=price.price_krw_per_l,
            distance_km=outbound.distance_km,
            fuel_volume_l=outbound.fuel_volume_l,
            fuel_cost_krw=outbound.fuel_cost_krw,
            one_way_krw=outbound.fuel_cost_krw,
            round_trip_complete=False,
            round_trip_distance_mode="unknown",
            confidence="stale" if price.source_status == "stale" else "estimated",
            reason=reason,
        )
