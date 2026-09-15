"""V0.9 trip-candidate assembly and cost completeness engine."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.accommodation.models import AccommodationOffer, AccommodationPriceBasis
from backend.entities.models import CanonicalPlace
from backend.fuel.models import DrivingCostResponse, DrivingCostResult
from backend.geo import haversine_distance_meters
from backend.models import Location
from backend.routing.identity import make_route_id
from backend.routing.models import RouteResult
from backend.trips.models import (
    CostComponent,
    CostComponentStatus,
    SourceDataStatus,
    TripCandidate,
    TripCandidateProvenance,
    TripCandidateRequest,
    TripCandidateResponse,
    TripCostBreakdown,
    TripCostStatus,
    TripQualityFeatures,
    TripType,
)

LOGGER = logging.getLogger(__name__)

MAX_ACCOMMODATION_PRICE_KRW = 1_000_000_000
DEFAULT_RESTAURANT_RADIUS_KM = 3.0
DEFAULT_ATTRACTION_RADIUS_KM = 20.0

_UNSET = object()


class TripAssemblyError(ValueError):
    """A dependency cannot be safely attached to the requested trip."""


@dataclass(frozen=True)
class AccommodationCostResult:
    amount_krw: int | None
    status: CostComponentStatus
    source: str | None
    fetched_at: datetime | None
    reason: str | None


@dataclass(frozen=True)
class CostEngineResult:
    breakdown: TripCostBreakdown
    warnings: tuple[str, ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def accommodation_offer_id(offer: AccommodationOffer) -> str:
    """Return a stable identity without treating two offers as one price."""

    if offer.source_offer_id:
        return f"{offer.source}:{offer.source_offer_id}"
    payload = offer.model_dump(mode="json", exclude={"price_freshness", "expires_at", "fetched_at"})
    return "offer_" + _fingerprint(payload)[:32]


def _dedupe_offers(offers: Iterable[AccommodationOffer]) -> list[AccommodationOffer]:
    """Collapse repeated source snapshots without merging distinct offer IDs."""

    freshness_rank = {"fresh": 3, "stale": 2, "unknown": 1, "expired": 0}
    by_identity: dict[tuple[str, str, str, int, int, str], AccommodationOffer] = {}
    for offer in offers:
        identity = (
            accommodation_offer_id(offer),
            offer.checkin.isoformat(),
            offer.checkout.isoformat(),
            offer.adults,
            offer.children,
            offer.place_source_id or "",
        )
        previous = by_identity.get(identity)
        if previous is None:
            by_identity[identity] = offer
            continue
        current_preference = (
            freshness_rank[offer.price_freshness],
            _utc(offer.fetched_at),
            _stable_json(offer.model_dump(mode="json")),
        )
        previous_preference = (
            freshness_rank[previous.price_freshness],
            _utc(previous.fetched_at),
            _stable_json(previous.model_dump(mode="json")),
        )
        if current_preference > previous_preference:
            by_identity[identity] = offer
    return [by_identity[key] for key in sorted(by_identity)]


def _freshness(offer: AccommodationOffer, now: datetime) -> str:
    reference = _utc(now)
    if offer.expires_at is not None:
        if _utc(offer.expires_at) <= reference:
            return "expired"
        if offer.price_freshness == "expired":
            return "expired"
    return offer.price_freshness


def calculate_accommodation_cost(
    offer: AccommodationOffer | None,
    *,
    nights: int,
    now: datetime | None = None,
) -> AccommodationCostResult:
    """Calculate only an explicitly scoped offer price.

    A numeric value with an unknown basis or unknown taxes is deliberately not
    promoted to a stay total.  Stale values remain usable only as estimates;
    expired values are not used in a total.
    """

    if offer is None:
        return AccommodationCostResult(
            None,
            CostComponentStatus.UNKNOWN,
            None,
            None,
            "ACCOMMODATION_OFFER_UNAVAILABLE",
        )
    if nights < 1:
        return AccommodationCostResult(
            None,
            CostComponentStatus.UNKNOWN,
            offer.source,
            offer.fetched_at,
            "ACCOMMODATION_NOT_REQUIRED_FOR_DAY_TRIP",
        )
    if offer.availability is False:
        return AccommodationCostResult(
            None,
            CostComponentStatus.UNKNOWN,
            offer.source,
            offer.fetched_at,
            "ACCOMMODATION_UNAVAILABLE",
        )

    if offer.final_price_krw is not None:
        source_amount = offer.final_price_krw
    elif offer.base_price_krw is not None and offer.taxes_krw is not None:
        source_amount = offer.base_price_krw + offer.taxes_krw
    elif offer.base_price_krw is not None:
        return AccommodationCostResult(
            None,
            CostComponentStatus.UNKNOWN,
            offer.source,
            offer.fetched_at,
            "ACCOMMODATION_TAXES_UNKNOWN",
        )
    else:
        return AccommodationCostResult(
            None,
            CostComponentStatus.UNKNOWN,
            offer.source,
            offer.fetched_at,
            "ACCOMMODATION_FINAL_PRICE_UNAVAILABLE",
        )

    if offer.price_basis is AccommodationPriceBasis.UNKNOWN:
        return AccommodationCostResult(
            None,
            CostComponentStatus.UNKNOWN,
            offer.source,
            offer.fetched_at,
            "ACCOMMODATION_PRICE_BASIS_UNKNOWN",
        )
    amount = source_amount if offer.price_basis is AccommodationPriceBasis.TOTAL_STAY else source_amount * nights
    if amount < 1 or amount > MAX_ACCOMMODATION_PRICE_KRW:
        return AccommodationCostResult(
            None,
            CostComponentStatus.UNKNOWN,
            offer.source,
            offer.fetched_at,
            "ACCOMMODATION_PRICE_OUT_OF_RANGE",
        )

    freshness = _freshness(offer, now or datetime.now(timezone.utc))
    if freshness == "expired":
        return AccommodationCostResult(
            None,
            CostComponentStatus.UNKNOWN,
            offer.source,
            offer.fetched_at,
            "ACCOMMODATION_OFFER_EXPIRED",
        )
    if freshness == "stale":
        return AccommodationCostResult(
            amount,
            CostComponentStatus.ESTIMATED,
            offer.source,
            offer.fetched_at,
            "ACCOMMODATION_OFFER_STALE",
        )
    if freshness == "unknown":
        return AccommodationCostResult(
            amount,
            CostComponentStatus.ESTIMATED,
            offer.source,
            offer.fetched_at,
            "ACCOMMODATION_OFFER_FRESHNESS_UNKNOWN",
        )
    return AccommodationCostResult(
        amount,
        CostComponentStatus.VERIFIED,
        offer.source,
        offer.fetched_at,
        None,
    )


def _driving_component(driving_cost: DrivingCostResult | None) -> CostComponent:
    if driving_cost is None:
        return CostComponent(
            name="driving",
            amount_krw=None,
            status=CostComponentStatus.UNKNOWN,
            reason="DRIVING_COST_UNAVAILABLE",
        )
    round_trip = driving_cost.round_trip
    if not round_trip.complete or round_trip.total_krw is None:
        return CostComponent(
            name="driving",
            amount_krw=None,
            status=CostComponentStatus.UNKNOWN,
            source="DrivingCostService",
            reason=driving_cost.reason or "DRIVING_COST_INCOMPLETE",
        )
    estimated = bool(driving_cost.contains_estimate or round_trip.status == "estimated")
    return CostComponent(
        name="driving",
        amount_krw=round_trip.total_krw,
        status=CostComponentStatus.ESTIMATED if estimated else CostComponentStatus.VERIFIED,
        source="DrivingCostService",
        reason=(driving_cost.reason or "DRIVING_COST_ESTIMATED") if estimated else None,
    )


class TripCostEngine:
    """Aggregate required cost components while preserving their evidence."""

    version = "v0.9.0"

    def calculate(
        self,
        *,
        driving_cost: DrivingCostResult | None,
        accommodation_offer: AccommodationOffer | None,
        nights: int,
        trip_type: TripType,
        now: datetime | None = None,
    ) -> TripCostBreakdown:
        return self.calculate_with_diagnostics(
            driving_cost=driving_cost,
            accommodation_offer=accommodation_offer,
            nights=nights,
            trip_type=trip_type,
            now=now,
        ).breakdown

    def calculate_with_diagnostics(
        self,
        *,
        driving_cost: DrivingCostResult | None,
        accommodation_offer: AccommodationOffer | None,
        nights: int,
        trip_type: TripType,
        now: datetime | None = None,
    ) -> CostEngineResult:
        if nights < 0:
            raise TripAssemblyError("nights cannot be negative")
        if trip_type is TripType.OVERNIGHT and nights < 1:
            raise TripAssemblyError("an overnight trip needs at least one night")
        if trip_type is TripType.DAY_TRIP and nights != 0:
            raise TripAssemblyError("a day trip must have zero nights")

        components = [_driving_component(driving_cost)]
        warnings: list[str] = []
        if components[0].reason:
            warnings.append(components[0].reason)
        required_components = ["driving"]
        if trip_type is TripType.OVERNIGHT:
            required_components.append("accommodation")
            accommodation = calculate_accommodation_cost(
                accommodation_offer,
                nights=nights,
                now=now,
            )
            components.append(
                CostComponent(
                    name="accommodation",
                    amount_krw=accommodation.amount_krw,
                    status=accommodation.status,
                    source=accommodation.source,
                    fetched_at=accommodation.fetched_at,
                    reason=accommodation.reason,
                )
            )
            if accommodation.reason:
                warnings.append(accommodation.reason)

        amounts = {component.name: component.amount_krw for component in components}
        missing = [name for name in required_components if amounts.get(name) is None]
        estimated = [component.name for component in components if component.status is CostComponentStatus.ESTIMATED]
        verified = [component.name for component in components if component.status is CostComponentStatus.VERIFIED]
        known_subtotal = sum(component.amount_krw for component in components if component.amount_krw is not None)
        complete = not missing
        total = sum(amounts[name] for name in required_components) if complete else None
        if complete:
            status = TripCostStatus.ESTIMATED_COMPLETE if estimated else TripCostStatus.VERIFIED_COMPLETE
        else:
            status = TripCostStatus.PARTIAL if known_subtotal else TripCostStatus.UNKNOWN
        breakdown = TripCostBreakdown(
            driving_krw=amounts.get("driving"),
            accommodation_krw=amounts.get("accommodation"),
            known_subtotal_krw=known_subtotal,
            total_krw=total,
            complete=complete,
            total_cost_complete=complete,
            status=status,
            required_components=required_components,
            components=components,
            missing_components=missing,
            estimated_components=estimated,
            verified_components=verified,
        )
        return CostEngineResult(breakdown=breakdown, warnings=tuple(dict.fromkeys(warnings)))


def _coerce_driving(value: Any) -> DrivingCostResult | None:
    if isinstance(value, DrivingCostResponse):
        return value.driving_cost
    if isinstance(value, DrivingCostResult):
        return value
    if value is None:
        return None
    return DrivingCostResult.model_validate(value)


def _validate_route_identity(
    route: RouteResult | None,
    driving_cost: DrivingCostResult | None,
    *,
    origin: Location,
    destination: Location,
) -> None:
    if route is None:
        if driving_cost is not None:
            raise TripAssemblyError("a driving cost needs a matching route")
        return
    expected_route_id = make_route_id(origin, destination, route)
    if route.route_id is not None and route.route_id != expected_route_id:
        raise TripAssemblyError("supplied route does not match the current origin and destination")
    if driving_cost is None:
        return
    outbound_id = driving_cost.outbound.route_id
    if outbound_id != route.route_id:
        raise TripAssemblyError("driving cost route does not match the supplied route")


def _dedupe_places(places: Iterable[CanonicalPlace]) -> list[CanonicalPlace]:
    by_id: dict[str, CanonicalPlace] = {}
    for place in places:
        if place.id not in by_id:
            by_id[place.id] = place
    return [by_id[key] for key in sorted(by_id)]


def _place_matches_offer(place: CanonicalPlace, offer: AccommodationOffer) -> bool:
    for membership in place.sources:
        if membership.source.casefold() != offer.source.casefold():
            continue
        if offer.place_source_id is not None and membership.source_id == offer.place_source_id:
            return True
    return False


def _find_accommodation(places: list[CanonicalPlace], offer: AccommodationOffer) -> CanonicalPlace | None:
    matches = [place for place in places if _place_matches_offer(place, offer)]
    if len(matches) == 1:
        return matches[0]
    if offer.place_source_id is None:
        source_matches = [
            place
            for place in places
            if any(membership.source.casefold() == offer.source.casefold() for membership in place.sources)
        ]
        if len(source_matches) == 1:
            return source_matches[0]
    return None


class _SpatialPlaceIndex:
    """Small category-local grid for nearby lookups during one assembly."""

    _KM_PER_LATITUDE_DEGREE = 111.2

    def __init__(self, places: list[CanonicalPlace], radius_km: float) -> None:
        self.radius_km = radius_km
        self.radius_degrees = radius_km / self._KM_PER_LATITUDE_DEGREE
        self.cell_degrees = max(self.radius_degrees, 0.0001)
        self.cells: dict[tuple[int, int], list[CanonicalPlace]] = {}
        self.unknown_coordinates: list[CanonicalPlace] = []
        for place in places:
            if place.lat is None or place.lng is None:
                self.unknown_coordinates.append(place)
                continue
            key = self._key(place.lat, place.lng)
            self.cells.setdefault(key, []).append(place)

    def _key(self, lat: float, lng: float) -> tuple[int, int]:
        return (
            math.floor(lat / self.cell_degrees),
            math.floor(lng / self.cell_degrees),
        )

    def nearby(
        self,
        *,
        reference: CanonicalPlace | None,
        destination: Location,
    ) -> list[CanonicalPlace]:
        if reference is not None and reference.lat is not None and reference.lng is not None:
            reference_location = Location(
                lat=reference.lat,
                lng=reference.lng,
                label=reference.name,
                source="canonical",
            )
        else:
            reference_location = destination

        center_lat, center_lng = reference_location.lat, reference_location.lng
        min_lat_cell = math.floor((center_lat - self.radius_degrees) / self.cell_degrees) - 1
        max_lat_cell = math.floor((center_lat + self.radius_degrees) / self.cell_degrees) + 1
        min_lng_cell = math.floor((center_lng - self.radius_degrees) / self.cell_degrees) - 1
        max_lng_cell = math.floor((center_lng + self.radius_degrees) / self.cell_degrees) + 1
        selected = list(self.unknown_coordinates)
        for lat_cell in range(min_lat_cell, max_lat_cell + 1):
            for lng_cell in range(min_lng_cell, max_lng_cell + 1):
                for place in self.cells.get((lat_cell, lng_cell), []):
                    place_location = Location(
                        lat=place.lat,
                        lng=place.lng,
                        label=place.name,
                        source="canonical",
                    )
                    if (
                        haversine_distance_meters(reference_location, place_location)
                        <= self.radius_km * 1000.0
                    ):
                        selected.append(place)
        return _dedupe_places(selected)


def _nearby(
    places: list[CanonicalPlace],
    *,
    reference: CanonicalPlace | None,
    destination: Location,
    radius_km: float,
    index: _SpatialPlaceIndex | None = None,
) -> list[CanonicalPlace]:
    if index is not None:
        return index.nearby(reference=reference, destination=destination)
    if reference is not None and reference.lat is not None and reference.lng is not None:
        reference_location = Location(
            lat=reference.lat,
            lng=reference.lng,
            label=reference.name,
            source="canonical",
        )
    else:
        reference_location = destination
    selected: list[CanonicalPlace] = []
    for place in places:
        if place.lat is None or place.lng is None:
            # A source-search result without coordinates is retained rather
            # than silently discarded; the quality feature exposes its
            # incomplete location metadata.
            selected.append(place)
            continue
        place_location = Location(
            lat=place.lat,
            lng=place.lng,
            label=place.name,
            source="canonical",
        )
        if haversine_distance_meters(reference_location, place_location) <= radius_km * 1000.0:
            selected.append(place)
    return _dedupe_places(selected)


def _mean_rating(places: Iterable[CanonicalPlace]) -> float | None:
    values = [place.normalized_rating for place in places if place.normalized_rating is not None]
    return sum(values) / len(values) if values else None


def _place_completeness(places: Iterable[CanonicalPlace]) -> float:
    materialized = list(places)
    if not materialized:
        return 0.0
    scores: list[float] = []
    for place in materialized:
        observed = sum(
            (
                bool(place.address),
                place.lat is not None and place.lng is not None,
                place.normalized_rating is not None,
                place.rating_confidence is not None,
                place.review_count_total is not None,
            )
        )
        scores.append(observed / 5.0)
    return sum(scores) / len(scores)


def _quality(
    *,
    accommodation: CanonicalPlace | None,
    restaurants: list[CanonicalPlace],
    attractions: list[CanonicalPlace],
    route: RouteResult | None,
    driving_cost: DrivingCostResult | None,
) -> TripQualityFeatures:
    round_trip = driving_cost.round_trip if driving_cost is not None else None
    distance_km = None
    if round_trip is not None and round_trip.distance_m is not None:
        distance_km = round_trip.distance_m / 1000.0
    elif route is not None:
        distance_km = route.distance_m / 1000.0
    duration_min = route.duration_s / 60.0 if route is not None else None
    all_places = [place for place in [accommodation, *restaurants, *attractions] if place is not None]
    return TripQualityFeatures(
        accommodation_rating=accommodation.normalized_rating if accommodation else None,
        accommodation_rating_confidence=accommodation.rating_confidence if accommodation else None,
        nearby_restaurant_count=len(restaurants),
        nearby_attraction_count=len(attractions),
        restaurant_rating_mean=_mean_rating(restaurants),
        attraction_rating_mean=_mean_rating(attractions),
        place_data_completeness=_place_completeness(all_places),
        has_accommodation=accommodation is not None,
        driving_distance_km=distance_km,
        driving_duration_min=duration_min,
    )


def _source_status(
    statuses: Mapping[str, SourceDataStatus],
    key: str,
    *,
    default: SourceDataStatus,
) -> SourceDataStatus:
    value = statuses.get(key, default)
    return value if isinstance(value, SourceDataStatus) else SourceDataStatus(value)


class TripCandidateService:
    """Assemble deterministic candidates from already fetched dependencies."""

    def __init__(
        self,
        *,
        cost_engine: TripCostEngine | None = None,
        restaurant_radius_km: float = DEFAULT_RESTAURANT_RADIUS_KM,
        attraction_radius_km: float = DEFAULT_ATTRACTION_RADIUS_KM,
    ) -> None:
        if restaurant_radius_km <= 0 or attraction_radius_km <= 0:
            raise ValueError("nearby radii must be positive")
        self.cost_engine = cost_engine or TripCostEngine()
        self.restaurant_radius_km = restaurant_radius_km
        self.attraction_radius_km = attraction_radius_km

    def assemble(
        self,
        request: TripCandidateRequest | Mapping[str, Any],
        *,
        route: RouteResult | None | object = _UNSET,
        driving_cost: DrivingCostResult | DrivingCostResponse | None | object = _UNSET,
        canonical_places: Iterable[CanonicalPlace] | None | object = _UNSET,
        accommodations: Iterable[CanonicalPlace] | None | object = _UNSET,
        restaurants: Iterable[CanonicalPlace] | None | object = _UNSET,
        attractions: Iterable[CanonicalPlace] | None | object = _UNSET,
        offers: Iterable[AccommodationOffer] | None | object = _UNSET,
        now: datetime | None = None,
    ) -> TripCandidateResponse:
        request_model = (
            request
            if isinstance(request, TripCandidateRequest)
            else TripCandidateRequest.model_validate(request)
        )
        reference_time = now or datetime.now(timezone.utc)
        route_value = request_model.route if route is _UNSET else route
        if route_value is not None and not isinstance(route_value, RouteResult):
            route_value = RouteResult.model_validate(route_value)
        if route_value is not None and route_value.route_id is None:
            route_value = route_value.model_copy(
                update={
                    "route_id": make_route_id(
                        request_model.origin,
                        request_model.destination,
                        route_value,
                    )
                }
            )
        driving_value = request_model.driving_cost if driving_cost is _UNSET else driving_cost
        driving_result = _coerce_driving(driving_value)
        _validate_route_identity(
            route_value,
            driving_result,
            origin=request_model.origin,
            destination=request_model.destination,
        )

        all_places_value = request_model.canonical_places if canonical_places is _UNSET else canonical_places
        all_places = _dedupe_places(all_places_value or [])
        accommodation_value = request_model.accommodations if accommodations is _UNSET else accommodations
        restaurant_value = request_model.restaurants if restaurants is _UNSET else restaurants
        attraction_value = request_model.attractions if attractions is _UNSET else attractions
        accommodation_places = _dedupe_places(
            [place for place in [*all_places, *(accommodation_value or [])] if place.category == "accommodation"]
        )
        restaurant_places = _dedupe_places(
            [place for place in [*all_places, *(restaurant_value or [])] if place.category == "restaurant"]
        )
        attraction_places = _dedupe_places(
            [place for place in [*all_places, *(attraction_value or [])] if place.category == "attraction"]
        )
        offers_value = request_model.offers if offers is _UNSET else offers
        # Offer order is source/cache incidental.  Normalize it before both
        # candidate ordering and dependency fingerprints so input reordering
        # cannot change a candidate's stable identity.
        offer_list = _dedupe_offers(offers_value or [])
        nights = (request_model.end_date - request_model.start_date).days
        trip_type = request_model.trip_type or (TripType.DAY_TRIP if nights == 0 else TripType.OVERNIGHT)

        statuses = dict(request_model.source_statuses)
        warnings: list[str] = list(request_model.source_warnings)
        if "driving" not in statuses:
            statuses["driving"] = (
                SourceDataStatus.OK
                if driving_result is not None and driving_result.round_trip.complete
                else SourceDataStatus.UNAVAILABLE
            )
        if "restaurants" not in statuses:
            statuses["restaurants"] = SourceDataStatus.OK if restaurant_places else SourceDataStatus.EMPTY
        if "attractions" not in statuses:
            statuses["attractions"] = SourceDataStatus.OK if attraction_places else SourceDataStatus.EMPTY
        if "accommodation" not in statuses:
            statuses["accommodation"] = (
                SourceDataStatus.NOT_REQUIRED
                if trip_type is TripType.DAY_TRIP
                else (SourceDataStatus.OK if offer_list else SourceDataStatus.UNAVAILABLE)
            )

        options: list[tuple[CanonicalPlace | None, AccommodationOffer | None]] = []
        if trip_type is TripType.DAY_TRIP:
            options.append((None, None))
        else:
            for offer in offer_list:
                if (
                    offer.checkin != request_model.start_date
                    or offer.checkout != request_model.end_date
                    or offer.adults != request_model.adults
                    or offer.children != request_model.children
                ):
                    warnings.append("ACCOMMODATION_OFFER_CONTEXT_MISMATCH")
                    continue
                accommodation = _find_accommodation(accommodation_places, offer)
                if accommodation is None:
                    warnings.append("ACCOMMODATION_OFFER_NOT_LINKED")
                    continue
                options.append((accommodation, offer))
            if not options:
                options.append((None, None))
                warnings.append(
                    "ACCOMMODATION_OFFER_UNAVAILABLE"
                    if not offer_list
                    else "NO_MATCHING_ACCOMMODATION_OFFER"
                )

        options.sort(
            key=lambda item: (
                item[0].id if item[0] is not None else "~missing-accommodation",
                accommodation_offer_id(item[1]) if item[1] is not None else "~missing-offer",
            )
        )
        options = options[: request_model.max_candidates]
        restaurant_index = _SpatialPlaceIndex(restaurant_places, self.restaurant_radius_km)
        attraction_index = _SpatialPlaceIndex(attraction_places, self.attraction_radius_km)

        request_payload = request_model.model_dump(mode="json", exclude={
            "route", "driving_cost", "canonical_places", "accommodations", "restaurants", "attractions", "offers"
        })
        dependency_payload = {
            "route": route_value.model_dump(mode="json") if route_value else None,
            "driving_cost": driving_result.model_dump(mode="json") if driving_result else None,
            "canonical_places": [place.model_dump(mode="json") for place in [*accommodation_places, *restaurant_places, *attraction_places]],
            "offers": [offer.model_dump(mode="json") for offer in offer_list],
        }
        request_fingerprint = _fingerprint({"request": request_payload, "dependencies": dependency_payload})
        candidate_identity_payload = {
            key: value
            for key, value in request_payload.items()
            if key not in {"source_statuses", "source_warnings", "max_candidates"}
        }
        # Explicit and inferred trip types describe the same candidate when
        # their dates agree; operational source state is provenance, not ID.
        candidate_identity_payload["trip_type"] = trip_type.value
        candidate_list: list[TripCandidate] = []
        for accommodation, offer in options:
            reference = accommodation if accommodation is not None else None
            nearby_restaurants = _nearby(
                restaurant_places,
                reference=reference,
                destination=request_model.destination,
                radius_km=self.restaurant_radius_km,
                index=restaurant_index,
            )
            nearby_attractions = _nearby(
                attraction_places,
                reference=reference,
                destination=request_model.destination,
                radius_km=self.attraction_radius_km,
                index=attraction_index,
            )
            cost_result = self.cost_engine.calculate_with_diagnostics(
                driving_cost=driving_result,
                accommodation_offer=offer,
                nights=nights,
                trip_type=trip_type,
                now=reference_time,
            )
            candidate_warnings = [*warnings, *cost_result.warnings]
            if accommodation is not None and offer is not None and accommodation.lat is None:
                candidate_warnings.append("ACCOMMODATION_COORDINATES_UNKNOWN")
            if not driving_result or not driving_result.round_trip.complete:
                candidate_warnings.append("DRIVING_COST_INCOMPLETE")
            candidate_warnings = list(dict.fromkeys(candidate_warnings))
            linked_places = [place for place in [accommodation, *nearby_restaurants, *nearby_attractions] if place is not None]
            offer_identity = accommodation_offer_id(offer) if offer is not None else None
            candidate_fingerprint = _fingerprint(
                {
                    "engine": self.cost_engine.version,
                    "base": candidate_identity_payload,
                    "accommodation_id": accommodation.id if accommodation else None,
                    "offer_id": offer_identity,
                }
            )
            source_updated = [membership.merged_at for place in linked_places for membership in place.sources]
            if offer is not None:
                source_updated.append(offer.fetched_at)
            source_updated_at = max((_utc(value) for value in source_updated), default=None)
            candidate = TripCandidate(
                id="tc_" + candidate_fingerprint[:32],
                origin=request_model.origin,
                destination=request_model.destination,
                start_date=request_model.start_date,
                end_date=request_model.end_date,
                nights=nights,
                trip_type=trip_type,
                adults=request_model.adults,
                children=request_model.children,
                route=route_value,
                accommodation=accommodation,
                accommodation_offer=offer,
                restaurants=nearby_restaurants,
                attractions=nearby_attractions,
                driving_cost=driving_result,
                costs=cost_result.breakdown,
                quality=_quality(
                    accommodation=accommodation,
                    restaurants=nearby_restaurants,
                    attractions=nearby_attractions,
                    route=route_value,
                    driving_cost=driving_result,
                ),
                complete=cost_result.breakdown.complete,
                warnings=candidate_warnings,
                component_statuses={
                    "driving": _source_status(statuses, "driving", default=SourceDataStatus.UNKNOWN),
                    "accommodation": _source_status(statuses, "accommodation", default=SourceDataStatus.UNKNOWN),
                    "restaurants": _source_status(statuses, "restaurants", default=SourceDataStatus.UNKNOWN),
                    "attractions": _source_status(statuses, "attractions", default=SourceDataStatus.UNKNOWN),
                },
                provenance=TripCandidateProvenance(
                    accommodation_canonical_id=accommodation.id if accommodation else None,
                    accommodation_offer_id=offer_identity,
                    driving_cost_route_id=(
                        driving_result.outbound.route_id if driving_result is not None else None
                    ),
                    canonical_place_ids=sorted(place.id for place in linked_places),
                    input_fingerprint=request_fingerprint,
                    source_updated_at=source_updated_at,
                ),
                created_at=reference_time,
            )
            candidate_list.append(candidate)

        response_complete = bool(candidate_list) and all(candidate.complete for candidate in candidate_list)
        response_warnings = list(dict.fromkeys(warnings + [warning for candidate in candidate_list for warning in candidate.warnings]))
        LOGGER.debug(
            "trip assembly candidates=%d complete=%d partial=%d known_total=%d "
            "missing_accommodation=%d estimated=%d",
            len(candidate_list),
            sum(candidate.complete for candidate in candidate_list),
            sum(not candidate.complete for candidate in candidate_list),
            sum(candidate.costs.total_krw is not None for candidate in candidate_list),
            sum("accommodation" in candidate.costs.missing_components for candidate in candidate_list),
            sum(bool(candidate.costs.estimated_components) for candidate in candidate_list),
        )
        return TripCandidateResponse(
            status="ok" if response_complete else "partial",
            complete=response_complete,
            candidates=candidate_list,
            component_statuses={
                "driving": _source_status(statuses, "driving", default=SourceDataStatus.UNKNOWN),
                "accommodation": _source_status(statuses, "accommodation", default=SourceDataStatus.UNKNOWN),
                "restaurants": _source_status(statuses, "restaurants", default=SourceDataStatus.UNKNOWN),
                "attractions": _source_status(statuses, "attractions", default=SourceDataStatus.UNKNOWN),
            },
            warnings=response_warnings,
            request_fingerprint=request_fingerprint,
            created_at=reference_time,
        )


__all__ = [
    "DEFAULT_ATTRACTION_RADIUS_KM",
    "DEFAULT_RESTAURANT_RADIUS_KM",
    "MAX_ACCOMMODATION_PRICE_KRW",
    "AccommodationCostResult",
    "TripAssemblyError",
    "TripCandidateService",
    "TripCostEngine",
    "accommodation_offer_id",
    "calculate_accommodation_cost",
]
