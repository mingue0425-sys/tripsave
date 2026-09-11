from backend.tolls.matcher import (
    coordinate_distance_meters,
    match_toll_gates,
    match_toll_gates_detailed,
)
from backend.tolls.models import TollGate


def gate(osm_id: int, name: str, lng: float, lat: float) -> TollGate:
    return TollGate(
        id=f"osm-node-{osm_id}",
        osm_type="node",
        osm_id=osm_id,
        name=name,
        normalized_name=name,
        lat=lat,
        lng=lng,
        gate_type="toll_booth",
    )


def test_route_matching_orders_gates_and_collapses_lane_duplicates() -> None:
    route = [(127.0, 36.0), (127.1, 36.0)]
    duplicate_offset_lng = 0.00025
    matches = match_toll_gates(
        route,
        [
            gate(2, "부산", 127.08, 36.0),
            gate(3, "부산", 127.080 + duplicate_offset_lng, 36.0),
            gate(1, "서울", 127.02, 36.0002),
            gate(4, "멀리", 127.08, 36.01),
        ],
        threshold_m=100.0,
        deduplication_threshold_m=150.0,
    )
    assert [match.gate.name for match in matches] == ["서울", "부산"]
    assert matches[0].position_along_route_m < matches[1].position_along_route_m
    assert matches[0].confidence in {"high", "medium"}
    assert coordinate_distance_meters((127.0, 36.0), (127.1, 36.0)) > 8_000


def test_gate_outside_threshold_is_not_inferred_as_route_gate() -> None:
    matches = match_toll_gates(
        [(127.0, 36.0), (127.1, 36.0)],
        [gate(1, "멀리", 127.05, 36.01)],
        threshold_m=100.0,
        deduplication_threshold_m=150.0,
    )
    assert matches == []


def test_detailed_matching_keeps_raw_candidates_separate_from_logical_gates() -> None:
    logical, raw = match_toll_gates_detailed(
        [(127.0, 36.0), (127.1, 36.0)],
        [
            gate(1, "서울", 127.02, 36.0),
            gate(2, "부산", 127.08, 36.0),
            gate(3, "부산", 127.08025, 36.0),
        ],
        threshold_m=100.0,
        deduplication_threshold_m=150.0,
    )

    assert len(raw) == 3
    assert len(logical) == 2
    assert sum(item.duplicate_count for item in logical) == 3
    assert len({item.duplicate_group for item in raw}) == 2
