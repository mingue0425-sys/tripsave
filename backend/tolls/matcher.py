"""Geometry-only matching primitives for route/toll evidence."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from backend.tolls.models import MatchedTollGate, TollGate
from backend.tolls.names import normalize_toll_name


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


@dataclass(frozen=True)
class _RouteSegment:
    """Precomputed geometry for one route segment."""

    first: Coordinate
    second: Coordinate
    length_m: float
    position_m: float


@dataclass(frozen=True)
class _RouteSegmentIndex:
    """Small uniform-grid index for repeated point-to-route projections."""

    segments: tuple[_RouteSegment, ...]
    grid: dict[tuple[int, int], tuple[int, ...]]
    long_segments: tuple[int, ...]
    scale_x: float
    scale_y: float
    cell_size_m: float
    threshold_m: float

    def candidate_segments(self, point: Coordinate) -> tuple[int, ...]:
        """Return every segment whose expanded box can contain ``point``."""

        x = point[0] * self.scale_x
        y = point[1] * self.scale_y
        x_start = math.floor((x - self.threshold_m) / self.cell_size_m)
        x_end = math.floor((x + self.threshold_m) / self.cell_size_m)
        y_start = math.floor((y - self.threshold_m) / self.cell_size_m)
        y_end = math.floor((y + self.threshold_m) / self.cell_size_m)

        candidates = set(self.long_segments)
        for cell_x in range(x_start, x_end + 1):
            for cell_y in range(y_start, y_end + 1):
                candidates.update(self.grid.get((cell_x, cell_y), ()))
        return tuple(sorted(candidates))


def _build_route_segment_index(
    route_coordinates: Sequence[Coordinate],
    gate_points: Sequence[Coordinate],
    *,
    threshold_m: float,
) -> _RouteSegmentIndex:
    """Build a conservative spatial index for route segment projections.

    Toll-gate matching used to scan every route segment for every gate. A
    uniform grid keeps the exact point-to-segment calculation, but limits it
    to segments whose expanded bounding box is near the gate. The longitude
    scale is intentionally the smallest scale present in the route/gates so
    the candidate filter cannot exclude a valid match.
    """

    if len(route_coordinates) < 2:
        raise ValueError("a route needs at least two coordinates")
    if threshold_m < 0 or not math.isfinite(threshold_m):
        raise ValueError("a route matching threshold must be finite and non-negative")

    latitudes = [point[1] for point in route_coordinates]
    latitudes.extend(point[1] for point in gate_points)
    scale_y = _meters_per_degree_latitude()
    scale_x = min(
        (_meters_per_degree_longitude(latitude) for latitude in latitudes),
        default=scale_y,
    )
    cell_size_m = max(threshold_m, 1.0)
    segments: list[_RouteSegment] = []
    grid: defaultdict[tuple[int, int], list[int]] = defaultdict(list)
    long_segments: list[int] = []
    cumulative_position = 0.0
    # A malformed or very long segment should not expand into an enormous
    # number of cells. It is rare in OSRM geometry and is checked directly.
    max_cells_per_segment = 4096

    for segment_index, (first, second) in enumerate(
        zip(route_coordinates, route_coordinates[1:])
    ):
        length_m = coordinate_distance_meters(first, second)
        segments.append(
            _RouteSegment(
                first=first,
                second=second,
                length_m=length_m,
                position_m=cumulative_position,
            )
        )
        cumulative_position += length_m

        first_x = first[0] * scale_x
        second_x = second[0] * scale_x
        first_y = first[1] * scale_y
        second_y = second[1] * scale_y
        x_start = math.floor((min(first_x, second_x) - threshold_m) / cell_size_m)
        x_end = math.floor((max(first_x, second_x) + threshold_m) / cell_size_m)
        y_start = math.floor((min(first_y, second_y) - threshold_m) / cell_size_m)
        y_end = math.floor((max(first_y, second_y) + threshold_m) / cell_size_m)
        cell_count = (x_end - x_start + 1) * (y_end - y_start + 1)
        if cell_count > max_cells_per_segment:
            long_segments.append(segment_index)
            continue
        for cell_x in range(x_start, x_end + 1):
            for cell_y in range(y_start, y_end + 1):
                grid[(cell_x, cell_y)].append(segment_index)

    return _RouteSegmentIndex(
        segments=tuple(segments),
        grid={cell: tuple(indices) for cell, indices in grid.items()},
        long_segments=tuple(long_segments),
        scale_x=scale_x,
        scale_y=scale_y,
        cell_size_m=cell_size_m,
        threshold_m=threshold_m,
    )


def _nearest_point_on_index(
    point: Coordinate,
    route_index: _RouteSegmentIndex,
) -> tuple[float, float]:
    """Project a point using the indexed route while preserving exact math."""

    candidates = route_index.candidate_segments(point)
    if not candidates:
        # Segment boxes are expanded by the complete matching threshold, so
        # an empty cell lookup proves that the point is outside the threshold.
        return math.inf, 0.0

    best_distance = math.inf
    best_position = 0.0
    for segment_index in candidates:
        segment = route_index.segments[segment_index]
        distance_m, fraction = point_to_segment_distance_meters(
            point, segment.first, segment.second
        )
        if distance_m < best_distance:
            best_distance = distance_m
            best_position = segment.position_m + segment.length_m * fraction
    return best_distance, best_position


def _confidence(distance_m: float, threshold_m: float) -> str:
    if distance_m <= min(50.0, threshold_m):
        return "high"
    if distance_m <= threshold_m * 0.6:
        return "medium"
    return "low"


def _compatible_context(first: TollGate, second: TollGate) -> bool:
    """Return whether two nearby features can be one logical tollgate.

    Missing OSM context is treated as unknown, not as a mismatch.  Explicitly
    different names, roads, or operators are kept separate even when their
    geometry is close (for example, parallel carriageways or a nearby ramp).
    """

    first_name = normalize_toll_name(first.normalized_name or first.name)
    second_name = normalize_toll_name(second.normalized_name or second.name)
    if first_name and second_name and first_name != second_name:
        return False
    if (
        first.road_name
        and second.road_name
        and normalize_toll_name(first.road_name)
        != normalize_toll_name(second.road_name)
    ):
        return False
    if (
        first.operator
        and second.operator
        and first.operator.casefold().strip() != second.operator.casefold().strip()
    ):
        return False
    return True


def project_toll_gate_candidates(
    route_coordinates: Sequence[Coordinate],
    gates: Sequence[TollGate],
    *,
    threshold_m: float,
) -> list[MatchedTollGate]:
    """Project every spatial candidate without treating it as a charge."""

    if not gates:
        return []
    route_index = _build_route_segment_index(
        route_coordinates,
        [(gate.lng, gate.lat) for gate in gates],
        threshold_m=threshold_m,
    )
    matches: list[MatchedTollGate] = []
    for gate in gates:
        distance_m, position_m = _nearest_point_on_index(
            (gate.lng, gate.lat), route_index
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
    return sorted(matches, key=lambda match: (match.position_along_route_m, match.distance_to_route_m))


def collapse_toll_gate_duplicates(
    matches: Sequence[MatchedTollGate], *, deduplication_threshold_m: float
) -> tuple[list[MatchedTollGate], list[MatchedTollGate]]:
    """Collapse lane/way duplicates and retain a fully annotated raw list."""

    groups: list[list[MatchedTollGate]] = []
    for match in sorted(matches, key=lambda item: (item.position_along_route_m, item.distance_to_route_m)):
        compatible_group: list[MatchedTollGate] | None = None
        for group in groups:
            anchor = min(
                group,
                key=lambda item: (item.distance_to_route_m, item.position_along_route_m),
            )
            position_gap = abs(match.position_along_route_m - anchor.position_along_route_m)
            location_gap = coordinate_distance_meters(
                (match.gate.lng, match.gate.lat), (anchor.gate.lng, anchor.gate.lat)
            )
            if (
                position_gap <= deduplication_threshold_m
                and location_gap <= deduplication_threshold_m
                and _compatible_context(match.gate, anchor.gate)
            ):
                compatible_group = group
                break
        if compatible_group is None:
            groups.append([match])
        else:
            compatible_group.append(match)

    annotated_raw: list[MatchedTollGate] = []
    representatives: list[MatchedTollGate] = []
    for index, group in enumerate(groups, start=1):
        group_id = f"tg-group-{index:03d}"
        representative = min(
            group,
            key=lambda match: (
                match.distance_to_route_m,
                0 if match.gate.name else 1,
                0 if match.gate.operator else 1,
            ),
        )
        for match in group:
            annotated = match.model_copy(
                update={
                    "duplicate_group": group_id,
                    "duplicate_count": len(group),
                }
            )
            annotated_raw.append(annotated)
        representatives.append(
            representative.model_copy(
                update={
                    "duplicate_group": group_id,
                    "duplicate_count": len(group),
                }
            )
        )
    annotated_raw.sort(key=lambda match: (match.position_along_route_m, match.distance_to_route_m))
    representatives.sort(key=lambda match: (match.position_along_route_m, match.distance_to_route_m))
    return representatives, annotated_raw


def match_toll_gates_detailed(
    route_coordinates: Sequence[Coordinate],
    gates: Sequence[TollGate],
    *,
    threshold_m: float,
    deduplication_threshold_m: float,
) -> tuple[list[MatchedTollGate], list[MatchedTollGate]]:
    """Return ``(logical_gates, raw_candidates)`` for diagnostics and API use."""

    projected = project_toll_gate_candidates(
        route_coordinates, gates, threshold_m=threshold_m
    )
    return collapse_toll_gate_duplicates(
        projected, deduplication_threshold_m=deduplication_threshold_m
    )


def match_toll_gates(
    route_coordinates: Sequence[Coordinate],
    gates: Sequence[TollGate],
    *,
    threshold_m: float,
    deduplication_threshold_m: float,
) -> list[MatchedTollGate]:
    """Project nearby OSM gate candidates and collapse lane duplicates."""

    representatives, _ = match_toll_gates_detailed(
        route_coordinates,
        gates,
        threshold_m=threshold_m,
        deduplication_threshold_m=deduplication_threshold_m,
    )
    return representatives
