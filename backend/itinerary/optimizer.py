"""Pure, deterministic multi-stop optimization over a fetched matrix."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from itertools import pairwise, permutations
from zoneinfo import ZoneInfo

from backend.routing.models import RouteGeometry

from .models import (
    MatrixEntry,
    OptimizationMode,
    OptimizedRoute,
    OptimizeRouteRequest,
    RouteSegment,
)

SEOUL = ZoneInfo("Asia/Seoul")


class ItineraryValidationError(ValueError):
    """The supplied matrix cannot describe a complete itinerary."""


def _matrix(entries: list[MatrixEntry] | None) -> dict[tuple[str, str], MatrixEntry]:
    return {(entry.from_id, entry.to_id): entry for entry in entries or []}


def _path_edges(path: tuple[str, ...], matrix: Mapping[tuple[str, str], MatrixEntry]) -> list[MatrixEntry] | None:
    edges: list[MatrixEntry] = []
    for from_id, to_id in pairwise(path):
        edge = matrix.get((from_id, to_id))
        if edge is None:
            return None
        edges.append(edge)
    return edges


def _path_key(
    path: tuple[str, ...],
    edges: list[MatrixEntry],
    mode: OptimizationMode,
) -> tuple[object, ...] | None:
    distance = sum(edge.distance_m for edge in edges)
    duration = sum(edge.duration_s for edge in edges)
    if mode is OptimizationMode.FASTEST:
        return (duration, distance, path)
    if mode is OptimizationMode.SHORTEST:
        return (distance, duration, path)
    # LOWEST_COST must never silently become FASTEST when pairwise fuel/toll
    # evidence is unavailable.  An incomplete path is ineligible for the cost
    # objective; callers receive an explicit infeasible/incomplete result.
    if any(not edge.cost_complete for edge in edges):
        return None
    total_cost = sum(
        edge.cost_krw for edge in edges if edge.cost_krw is not None
    )
    return (total_cost, duration, distance, path)


def _choose_path(
    request: OptimizeRouteRequest,
    matrix: Mapping[tuple[str, str], MatrixEntry],
) -> tuple[tuple[str, ...], list[MatrixEntry]] | None:
    origin_id = request.origin.id
    destination_id = request.destination.id
    waypoint_ids = tuple(waypoint.id for waypoint in request.waypoints)
    if not waypoint_ids:
        path = (origin_id, destination_id)
        edges = _path_edges(path, matrix)
        if edges is None:
            return None
        key = _path_key(path, edges, request.mode)
        return (path, edges) if key is not None else None

    if len(waypoint_ids) <= 8:
        candidates: list[tuple[tuple[object, ...], tuple[str, ...], list[MatrixEntry]]] = []
        for permutation in permutations(waypoint_ids):
            path = (origin_id, *permutation, destination_id)
            edges = _path_edges(path, matrix)
            if edges is not None:
                key = _path_key(path, edges, request.mode)
                if key is not None:
                    candidates.append((key, path, edges))
        if not candidates:
            return None
        _key, path, edges = min(candidates, key=lambda candidate: candidate[0])
        return path, edges

    # Deterministic nearest-neighbour construction followed by deterministic
    # 2-opt.  It bounds work for the configured 20-waypoint maximum.
    remaining = set(waypoint_ids)
    path_list = [origin_id]
    while remaining:
        current = path_list[-1]
        choices: list[tuple[tuple[object, ...], str, MatrixEntry]] = []
        for candidate in sorted(remaining):
            edge = matrix.get((current, candidate))
            if edge is not None:
                key = _path_key((current, candidate), [edge], request.mode)
                if key is not None:
                    choices.append((key, candidate, edge))
        if not choices:
            return None
        _key, chosen, _edge = min(choices, key=lambda item: item[0])
        path_list.append(chosen)
        remaining.remove(chosen)
    path_list.append(destination_id)

    def current_key(candidate_path: list[str]) -> tuple[object, ...] | None:
        edges = _path_edges(tuple(candidate_path), matrix)
        return _path_key(tuple(candidate_path), edges, request.mode) if edges is not None else None

    best_path = path_list
    best_key = current_key(best_path)
    if best_key is None:
        return None
    improved = True
    while improved:
        improved = False
        for first in range(1, len(best_path) - 2):
            for last in range(first + 1, len(best_path) - 1):
                candidate_path = best_path[:first] + list(reversed(best_path[first : last + 1])) + best_path[last + 1 :]
                candidate_key = current_key(candidate_path)
                if candidate_key is not None and candidate_key < best_key:
                    best_path, best_key = candidate_path, candidate_key
                    improved = True
    edges = _path_edges(tuple(best_path), matrix)
    return (tuple(best_path), edges) if edges is not None else None


def _normalise_start(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=SEOUL) if value.tzinfo is None else value.astimezone(SEOUL)


def _build_geometry(segments: list[RouteSegment]) -> RouteGeometry | None:
    coordinates: list[list[float]] = []
    for segment in segments:
        if segment.geometry is None:
            return None
        points = segment.geometry.coordinates
        if coordinates and coordinates[-1] == points[0]:
            coordinates.extend(points[1:])
        else:
            coordinates.extend(points)
    return RouteGeometry(type="LineString", coordinates=coordinates) if len(coordinates) >= 2 else None


def optimize_matrix(request: OptimizeRouteRequest, entries: list[MatrixEntry]) -> OptimizedRoute:
    """Choose a route and calculate cost/ETA without any external I/O."""

    matrix = _matrix(entries)
    chosen = _choose_path(request, matrix)
    if chosen is None:
        point_count = len(request.waypoints) + 2
        expected_directed_edges = point_count * (point_count - 1)
        has_full_route_matrix = len(matrix) == expected_directed_edges
        warning = "ROUTE_MATRIX_INCOMPLETE"
        if (
            request.mode is OptimizationMode.LOWEST_COST
            and has_full_route_matrix
            and any(not edge.cost_complete for edge in matrix.values())
        ):
            warning = "ROUTE_COST_MATRIX_INCOMPLETE"
        return OptimizedRoute(
            mode=request.mode,
            origin=request.origin,
            destination=request.destination,
            ordered_waypoints=list(request.waypoints),
            feasible=False,
            cost_complete=False,
            warnings=[warning],
            start_datetime=_normalise_start(request.start_datetime),
        )

    path, edges = chosen
    waypoint_by_id = {waypoint.id: waypoint for waypoint in request.waypoints}
    start = _normalise_start(request.start_datetime)
    clock = start
    segments: list[RouteSegment] = []
    total_distance = sum(edge.distance_m for edge in edges)
    total_duration = sum(edge.duration_s for edge in edges)
    has_unknown_cost = False
    has_estimated_cost = False
    for index, edge in enumerate(edges):
        to_id = path[index + 1]
        waypoint = waypoint_by_id.get(to_id)
        visit_duration = waypoint.visit_duration_min if waypoint is not None else 0
        arrival = clock + timedelta(seconds=edge.duration_s) if clock is not None else None
        next_departure = (
            arrival + timedelta(minutes=visit_duration) if arrival is not None else None
        )
        cost_status = "unknown"
        if edge.cost_complete:
            cost_status = "estimated" if edge.cost_is_estimated else "verified"
            has_estimated_cost = has_estimated_cost or cost_status == "estimated"
        else:
            has_unknown_cost = True
        segments.append(
            RouteSegment(
                from_id=path[index],
                to_id=to_id,
                distance_m=edge.distance_m,
                duration_s=edge.duration_s,
                departure_time=clock,
                arrival_time=arrival,
                next_departure_time=next_departure,
                visit_duration_min=visit_duration,
                fuel_krw=edge.fuel_krw,
                toll_krw=edge.toll_krw,
                fuel_status=edge.fuel_status,
                toll_status=edge.toll_status,
                cost_status=cost_status,
            )
        )
        clock = next_departure

    cost = sum(edge.cost_krw for edge in edges) if not has_unknown_cost else None
    cost_status = "unknown"
    if cost is not None:
        cost_status = "estimated" if has_estimated_cost else "verified"
    warnings: list[str] = []
    if has_unknown_cost:
        warnings.append("ROUTE_COST_INCOMPLETE")
    if request.max_daily_driving_min is not None and total_duration / 60.0 > request.max_daily_driving_min:
        warnings.append("MAX_DAILY_DRIVING_EXCEEDED")
    feasible = not warnings or warnings == ["ROUTE_COST_INCOMPLETE"]
    if "MAX_DAILY_DRIVING_EXCEEDED" in warnings:
        feasible = False
    ordered = [waypoint_by_id[point_id] for point_id in path[1:-1]]
    return OptimizedRoute(
        mode=request.mode,
        origin=request.origin,
        destination=request.destination,
        ordered_waypoints=ordered,
        segments=segments,
        total_distance_m=total_distance,
        total_duration_s=total_duration,
        total_cost_krw=cost,
        cost_complete=cost is not None,
        cost_status=cost_status,
        geometry=_build_geometry(segments),
        feasible=feasible,
        warnings=warnings,
        start_datetime=start,
    )
