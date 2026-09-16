from datetime import datetime
from zoneinfo import ZoneInfo

from backend.itinerary.models import (
    MatrixEntry,
    OptimizationMode,
    OptimizeRouteRequest,
    RoutePoint,
    RouteWaypoint,
)
from backend.itinerary.optimizer import optimize_matrix


def _points():
    return (
        RoutePoint(id="O", name="출발", lat=36.0, lng=127.0),
        RoutePoint(id="D", name="도착", lat=36.2, lng=127.2),
        [
            RouteWaypoint(id="A", name="A", lat=36.03, lng=127.03, visit_duration_min=60),
            RouteWaypoint(id="B", name="B", lat=36.06, lng=127.06),
            RouteWaypoint(id="C", name="C", lat=36.09, lng=127.09),
        ],
    )


def _matrix(points, *, preferred, cost_path=None, unknown_cost=False):
    origin, destination, waypoints = points
    ids = [origin.id, *(waypoint.id for waypoint in waypoints), destination.id]
    entries = []
    for from_id in ids:
        for to_id in ids:
            if from_id == to_id:
                continue
            rank = preferred.get((from_id, to_id), 100)
            entry = {
                "from_id": from_id,
                "to_id": to_id,
                "distance_m": float(rank * 10),
                "duration_s": float(rank * 10),
            }
            if not unknown_cost:
                cost = (cost_path or {}).get((from_id, to_id), rank * 100)
                entry.update({"fuel_krw": cost, "toll_krw": 0})
            entries.append(MatrixEntry(**entry))
    return entries


def _request(points, mode, waypoints=None, **kwargs):
    origin, destination, default_waypoints = points
    return OptimizeRouteRequest(
        origin=origin,
        destination=destination,
        waypoints=waypoints if waypoints is not None else default_waypoints,
        mode=mode,
        **kwargs,
    )


def test_exact_modes_choose_their_own_objective():
    points = _points()
    fastest_preference = {
        ("O", "A"): 1, ("A", "B"): 1, ("B", "C"): 1, ("C", "D"): 1,
        ("O", "C"): 2, ("C", "B"): 2, ("B", "A"): 2, ("A", "D"): 2,
    }
    shortest_preference = {
        ("O", "C"): 1, ("C", "A"): 1, ("A", "B"): 1, ("B", "D"): 1,
    }
    fastest = optimize_matrix(
        _request(points, OptimizationMode.FASTEST),
        _matrix(points, preferred=fastest_preference),
    )
    shortest = optimize_matrix(
        _request(points, OptimizationMode.SHORTEST),
        _matrix(points, preferred=shortest_preference),
    )
    assert [waypoint.id for waypoint in fastest.ordered_waypoints] == ["A", "B", "C"]
    assert [waypoint.id for waypoint in shortest.ordered_waypoints] == ["C", "A", "B"]
    assert fastest.feasible is True
    assert shortest.feasible is True


def test_lowest_cost_does_not_treat_unknown_as_zero():
    points = _points()
    cost_preference = {
        ("O", "B"): 1, ("B", "A"): 1, ("A", "C"): 1, ("C", "D"): 1,
    }
    result = optimize_matrix(
        _request(points, OptimizationMode.LOWEST_COST),
        _matrix(points, preferred=cost_preference, unknown_cost=True),
    )
    assert result.total_cost_krw is None
    assert result.cost_complete is False
    assert "ROUTE_COST_INCOMPLETE" in result.warnings

    complete = optimize_matrix(
        _request(points, OptimizationMode.LOWEST_COST),
        _matrix(points, preferred={}, cost_path=cost_preference),
    )
    assert complete.total_cost_krw is not None
    assert complete.cost_complete is True


def test_eta_and_visit_duration_are_local_time_deterministic():
    points = _points()
    request = _request(
        points,
        OptimizationMode.FASTEST,
        waypoints=[points[2][0]],
        start_datetime=datetime(2026, 10, 3, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    result = optimize_matrix(
        request,
        [
            MatrixEntry(from_id="O", to_id="A", distance_m=1, duration_s=1_800),
            MatrixEntry(from_id="A", to_id="D", distance_m=1, duration_s=1_800),
        ],
    )
    assert result.segments[0].arrival_time.isoformat() == "2026-10-03T09:30:00+09:00"
    assert result.segments[0].next_departure_time.isoformat() == "2026-10-03T10:30:00+09:00"
    assert result.segments[1].arrival_time.isoformat() == "2026-10-03T11:00:00+09:00"


def test_daily_limit_marks_result_infeasible_without_zeroing_route():
    points = _points()
    result = optimize_matrix(
        _request(points, OptimizationMode.FASTEST, max_daily_driving_min=1),
        _matrix(points, preferred={}),
    )
    assert result.feasible is False
    assert result.total_duration_s is not None
    assert "MAX_DAILY_DRIVING_EXCEEDED" in result.warnings


def test_missing_edge_is_infeasible_not_zero_distance():
    points = _points()
    result = optimize_matrix(
        _request(points, OptimizationMode.FASTEST),
        [],
    )
    assert result.feasible is False
    assert result.total_distance_m is None
    assert result.total_duration_s is None
    assert "ROUTE_MATRIX_INCOMPLETE" in result.warnings


def test_waypoint_input_order_does_not_change_exact_result():
    points = _points()
    entries = _matrix(points, preferred={("O", "A"): 1, ("A", "B"): 1, ("B", "C"): 1, ("C", "D"): 1})
    first = optimize_matrix(_request(points, OptimizationMode.FASTEST), entries)
    shuffled = [points[2][2], points[2][0], points[2][1]]
    second = optimize_matrix(_request(points, OptimizationMode.FASTEST, waypoints=shuffled), entries)
    assert [item.id for item in first.ordered_waypoints] == [item.id for item in second.ordered_waypoints]
