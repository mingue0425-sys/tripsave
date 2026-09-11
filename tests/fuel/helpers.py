from datetime import datetime, timezone

from backend.fuel.models import FuelPriceResult, FuelType
from backend.models import Location
from backend.routing.models import RouteGeometry, RouteResult
from backend.tolls.models import TollResult, TollVehicleClass


def make_route(
    *,
    distance_m: float = 100_000.0,
    origin: Location | None = None,
    destination: Location | None = None,
    route_id: str | None = None,
) -> tuple[Location, Location, RouteResult]:
    origin = origin or Location(lat=36.0, lng=127.0)
    destination = destination or Location(lat=36.0, lng=127.1)
    route = RouteResult(
        distance_m=distance_m,
        duration_s=3_600.0,
        geometry=RouteGeometry(
            type="LineString",
            coordinates=[
                [origin.lng, origin.lat],
                [(origin.lng + destination.lng) / 2, (origin.lat + destination.lat) / 2],
                [destination.lng, destination.lat],
            ],
        ),
        route_id=route_id,
    )
    return origin, destination, route


def make_price(
    fuel_type: FuelType = FuelType.GASOLINE,
    *,
    price: float = 1_700.0,
    source_status: str = "fresh",
) -> FuelPriceResult:
    return FuelPriceResult(
        fuel_type=fuel_type,
        price_krw_per_l=price,
        source_url="https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
        observed_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        source_status=source_status,
        complete=True,
        raw_evidence_hash="a" * 64,
    )


def make_toll(
    *,
    total: int | None = 10_000,
    route_id: str = "route-test",
    vehicle_class: TollVehicleClass = TollVehicleClass.CLASS_1,
) -> TollResult:
    complete = total is not None
    return TollResult(
        status="ok" if complete else "partial",
        complete=complete,
        vehicle_class=vehicle_class,
        total_toll_krw=total,
        known_toll_krw=total if complete else None,
        route_id=route_id,
        source_status="fresh" if complete else "unavailable",
        fetched_at=datetime(2026, 9, 11, tzinfo=timezone.utc) if complete else None,
    )
