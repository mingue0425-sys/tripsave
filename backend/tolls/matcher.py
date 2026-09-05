"""Geometry-only matching primitives for route/toll evidence."""

from __future__ import annotations

import math
from collections.abc import Sequence

from backend.tolls.models import MatchedTollGate, TollGate


Coordinate = tuple[float, float]  # canonical GeoJSON boundary: (lng, lat)
EARTH_RADIUS_METERS = 6_371_008.8


def _meters_per_degree_latitude() -> float:
    return math.pi * EARTH_RADIUS_METERS / 180.0


def _meters_per_degree_longitude(latitude: float) -> float:
    return _meters_per_degree_latitude() * max(0.01, math.cos(math.radians(latitude)))


def point_to_segment_distance_meters(
    point: Coordinate,
    first: Coordinate,
    second: Coordinate,
) -> tuple[float, float]:
    """Return distance and segment fraction using a local projection."""

    reference_latitude = (point[1] + first[1] + second[1]) / 3.0
    scale_x = _meters_per_degree_longitude(reference_latitude)
    scale_y = _meters_per_degree_latitude()
    first_x = (first[0] - point[0]) * scale_x
    first_y = (first[1] - point[1]) * scale_y
    second_x = (second[0] - point[0]) * scale_x
    second_y = (second[1] - point[1]) * scale_y
    delta_x = second_x - first_x
    delta_y = second_y - first_y
    segment_squared = delta_x * delta_x + delta_y * delta_y
    if segment_squared == 0:
        return math.hypot(first_x, first_y), 0.0
    fraction = max(
        0.0,
        min(1.0, (-(first_x * delta_x + first_y * delta_y)) / segment_squared),
    )
    closest_x = first_x + fraction * delta_x
    closest_y = first_y + fraction * delta_y
    return math.hypot(closest_x, closest_y), fraction


def coordinate_distance_meters(first: Coordinate, second: Coordinate) -> float:
    """Return a local equirectangular distance between two nearby coordinates."""

    reference_latitude = (first[1] + second[1]) / 2.0
    delta_x = (second[0] - first[0]) * _meters_per_degree_longitude(reference_latitude)
    delta_y = (second[1] - first[1]) * _meters_per_degree_latitude()
    return math.hypot(delta_x, delta_y)


def nearest_point_on_route(
    point: Coordinate,
    route_coordinates: Sequence[Coordinate],
) -> tuple[float, float]:
    """Return ``(distance_m, position_along_route_m)`` for a point."""

    if len(route_coordinates) < 2:
        raise ValueError("a route needs at least two coordinates")
    best_distance = math.inf
    best_position = 0.0
    cumulative = 0.0
    for first, second in zip(route_coordinates, route_coordinates[1:]):
        segment_distance, fraction = point_to_segment_distance_meters(
            point, first, second
        )
        segment_length = coordinate_distance_meters(first, second)
        if segment_distance < best_distance:
            best_distance = segment_distance
            best_position = cumulative + segment_length * fraction
        cumulative += segment_length
    return best_distance, best_position


def _confidence(distance_m: float, threshold_m: float) -> str:
    if distance_m <= min(50.0, threshold_m):
        return "high"
    if distance_m <= threshold_m * 0.6:
        return "medium"
    return "low"


def match_toll_gates(
    route_coordinates: Sequence[Coordinate],
    gates: Sequence[TollGate],
    *,
    threshold_m: float,
    deduplication_threshold_m: float,
) -> list[MatchedTollGate]:
    """Project nearby OSM gate candidates and collapse lane duplicates."""

    matches: list[MatchedTollGate] = []
    for gate in gates:
        distance_m, position_m = nearest_point_on_route(
            (gate.lng, gate.lat), route_coordinates
        )
        if distance_m <= threshold_m:
            matches.append(
                MatchedTollGate(
                    gate=gate,
                    distance_to_route_m=distance_m,
                    position_along_route_m=position_m,
                    confidence=_confidence(distance_m, threshold_m),
                )
            )

    matches.sort(key=lambda match: (match.position_along_route_m, match.distance_to_route_m))
    groups: list[list[MatchedTollGate]] = []
    for match in matches:
        if not groups:
            groups.append([match])
            continue
        previous = groups[-1][-1]
        position_gap = match.position_along_route_m - previous.position_along_route_m
        location_gap = math.hypot(
            (match.gate.lng - previous.gate.lng)
            * _meters_per_degree_longitude(match.gate.lat),
            (match.gate.lat - previous.gate.lat) * _meters_per_degree_latitude(),
        )
        names_compatible = (
            not match.gate.normalized_name
            or not previous.gate.normalized_name
            or match.gate.normalized_name == previous.gate.normalized_name
        )
        if (
            position_gap <= deduplication_threshold_m
            and location_gap <= deduplication_threshold_m
            and names_compatible
        ):
            groups[-1].append(match)
        else:
            groups.append([match])

    representatives: list[MatchedTollGate] = []
    for group in groups:
        representatives.append(
            min(
                group,
                key=lambda match: (
                    match.distance_to_route_m,
                    0 if match.gate.name else 1,
                ),
            )
        )
    return representatives
