from datetime import datetime, timezone

from backend.accommodation.models import AccommodationOffer, AccommodationPriceBasis
from backend.entities.models import CanonicalPlace, SourceMembership
from backend.fuel.models import (
    DrivingCostLeg,
    DrivingCostResult,
    FuelType,
    RoundTripToll,
)
from backend.models import Location
from backend.routing.identity import make_route_id
from backend.routing.models import RouteGeometry, RouteResult
from backend.tolls.models import TollVehicleClass
from backend.trips.models import TripCandidateRequest, VehicleProfile

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def make_locations() -> tuple[Location, Location]:
    return (
        Location(lat=37.5665, lng=126.9780, label="Seoul"),
        Location(lat=35.1796, lng=129.0756, label="Busan"),
    )


def make_route() -> tuple[Location, Location, RouteResult]:
    origin, destination = make_locations()
    route = RouteResult(
        distance_m=400_000.0,
        duration_s=18_000.0,
        route_id=None,
        geometry=RouteGeometry(
            type="LineString",
            coordinates=[
                [origin.lng, origin.lat],
                [destination.lng, destination.lat],
            ],
        ),
    )
    return origin, destination, route.model_copy(
        update={"route_id": make_route_id(origin, destination, route)}
    )


def make_driving_cost(
    *,
    total: int = 44_000,
    estimated: bool = False,
    route_id: str | None = None,
) -> DrivingCostResult:
    if route_id is None:
        route_id = make_route()[2].route_id
    outbound_total = total // 2
    return_total = total - outbound_total
    status = "estimated" if estimated else "verified"
    toll_mode = "estimated_doubled_outbound" if estimated else "verified_official"
    toll_verified = not estimated
    return DrivingCostResult(
        complete=True,
        cost_complete=True,
        outbound=DrivingCostLeg(
            complete=True,
            route_id=route_id,
            distance_m=200_000.0,
            fuel_volume_l=10.0,
            fuel_cost_krw=outbound_total - 5_000,
            toll_krw=5_000,
            total_krw=outbound_total,
            status=status,
            fuel_status=status,
            toll_status="estimated" if estimated else "verified",
            toll_mode=toll_mode,
            toll_verified=toll_verified,
        ),
        return_leg=DrivingCostLeg(
            complete=True,
            route_id="return-route-seoul-busan",
            distance_m=200_000.0,
            fuel_volume_l=10.0,
            fuel_cost_krw=return_total - 5_000,
            toll_krw=5_000,
            total_krw=return_total,
            status=status,
            fuel_status=status,
            toll_status="estimated" if estimated else "verified",
            toll_mode=toll_mode,
            toll_verified=toll_verified,
        ),
        round_trip=DrivingCostLeg(
            complete=True,
            route_id=None,
            distance_m=400_000.0,
            fuel_volume_l=20.0,
            fuel_cost_krw=total - 10_000,
            toll_krw=10_000,
            total_krw=total,
            status=status,
            fuel_status=status,
            toll_status="estimated" if estimated else "verified",
            toll_mode=toll_mode,
            toll_verified=toll_verified,
        ),
        round_trip_toll=RoundTripToll(
            amount_krw=10_000,
            mode=toll_mode,
            verified=not estimated,
            estimated=estimated,
            complete=not estimated,
            reason="RETURN_TOLL_UNAVAILABLE" if estimated else None,
        ),
        officially_verified=not estimated,
        contains_estimate=estimated,
        round_trip_distance_mode="doubled_one_way" if estimated else "reverse_route",
    )


def make_place(
    place_id: str,
    name: str,
    category: str,
    *,
    source: str = "visitkorea",
    source_id: str | None = None,
    lat: float | None = 35.1796,
    lng: float | None = 129.0756,
    address: str | None = "Busan address",
    rating: float | None = 0.9,
    rating_confidence: float | None = 0.8,
    review_count: int | None = 100,
) -> CanonicalPlace:
    source_id = source_id or place_id
    membership = SourceMembership(
        source=source,
        source_id=source_id,
        source_url=(
            "https://english.visitkorea.or.kr/detail"
            if source == "visitkorea"
            else "https://www.booking.com/hotel/kr/sample.html"
        ),
        source_record_id=f"record-{place_id}",
        source_record_fingerprint="a" * 64,
        match_score=1.0,
        match_method="SINGLE_SOURCE_CANONICAL",
        source_confidence=0.9,
        merged_at=NOW,
    )
    return CanonicalPlace(
        id=place_id,
        name=name,
        category=category,
        lat=lat,
        lng=lng,
        address=address,
        normalized_rating=rating,
        rating_confidence=rating_confidence,
        review_count_total=review_count,
        observed_review_count_sum=review_count,
        source_count=1,
        confidence=0.9,
        sources=[membership],
        aliases=[name],
        raw_fields={},
        coordinate_conflict=False,
        rating_disagreement=False,
        decision_trace=[],
        created_at=NOW,
        updated_at=NOW,
    )


def make_offer(
    *,
    source_id: str = "hotel-1",
    source_offer_id: str = "offer-1",
    final_price_krw: int | None = 240_000,
    price_basis: AccommodationPriceBasis | str = AccommodationPriceBasis.TOTAL_STAY,
    adults: int = 2,
    children: int = 0,
    checkin="2026-10-01",
    checkout="2026-10-03",
    price_freshness: str = "fresh",
    base_price_krw: int | None = None,
    taxes_krw: int | None = None,
) -> AccommodationOffer:
    from datetime import date

    return AccommodationOffer(
        source="booking",
        source_offer_id=source_offer_id,
        place_source_id=source_id,
        checkin=date.fromisoformat(checkin),
        checkout=date.fromisoformat(checkout),
        adults=adults,
        children=children,
        room_name="Room",
        base_price_krw=base_price_krw,
        taxes_krw=taxes_krw,
        final_price_krw=final_price_krw,
        price_basis=price_basis,
        price_freshness=price_freshness,
        availability=True,
        fetched_at=NOW,
    )


def make_request(**updates) -> TripCandidateRequest:
    origin, destination, route = make_route()
    value = {
        "origin": origin,
        "destination": destination,
        "start_date": "2026-10-01",
        "end_date": "2026-10-03",
        "adults": 2,
        "children": 0,
        "vehicle": VehicleProfile(
            fuel_type=FuelType.GASOLINE,
            fuel_efficiency_km_per_l=13.5,
            vehicle_class=TollVehicleClass.CLASS_1,
        ),
        "route": route,
        "driving_cost": make_driving_cost(),
    }
    value.update(updates)
    return TripCandidateRequest.model_validate(value)
