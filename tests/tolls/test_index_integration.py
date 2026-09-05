import asyncio
import json
from pathlib import Path

import pytest

from backend.models import Location
from backend.routing.osrm import OSRMClient
from backend.tolls.index import TollIndex


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_real_osrm_route_matches_real_pbf_toll_evidence() -> None:
    places = {
        place["id"]: Location(
            lat=place["lat"],
            lng=place["lng"],
            label=place["name"],
            source="local_search",
        )
        for place in json.loads(
            (PROJECT_ROOT / "static" / "data" / "places.json").read_text(
                encoding="utf-8"
            )
        )
    }

    async def build_route():
        return await OSRMClient().route(places["city-seoul"], places["city-busan"])

    route = asyncio.run(build_route())
    index = TollIndex(PROJECT_ROOT / "data" / "korea_trip.db")
    try:
        analysis = index.analyze_route([tuple(point) for point in route.geometry.coordinates])
    finally:
        index.close()

    assert analysis.toll_road_detected is True
    assert analysis.toll_roads
    assert analysis.gates
    assert all(
        first.position_along_route_m <= second.position_along_route_m
        for first, second in zip(analysis.gates, analysis.gates[1:])
    )
    assert any(road.operator and "한국도로공사" in road.operator for road in analysis.toll_roads)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("origin_id", "destination_id", "expect_toll_evidence"),
    [
        ("city-seoul", "city-incheon", True),
        ("city-seoul", "city-daejeon", True),
        ("city-busan", "city-daegu", True),
        ("city-busan", "city-gyeongju", True),
        ("city-daejeon", "city-gwangju", True),
        ("city-gangneung", "city-seoul", True),
        ("city-jeju", "city-seogwipo", False),
    ],
)
def test_representative_routes_have_conservative_toll_analysis(
    origin_id: str,
    destination_id: str,
    expect_toll_evidence: bool,
) -> None:
    places = {
        place["id"]: Location(
            lat=place["lat"],
            lng=place["lng"],
            label=place["name"],
            source="local_search",
        )
        for place in json.loads(
            (PROJECT_ROOT / "static" / "data" / "places.json").read_text(
                encoding="utf-8"
            )
        )
    }

    async def build_route():
        return await OSRMClient().route(places[origin_id], places[destination_id])

    route = asyncio.run(build_route())
    index = TollIndex(PROJECT_ROOT / "data" / "korea_trip.db")
    try:
        analysis = index.analyze_route(
            [tuple(point) for point in route.geometry.coordinates]
        )
    finally:
        index.close()

    assert route.distance_m > 0
    assert route.duration_s > 0
    assert len(route.geometry.coordinates) >= 2
    assert all(
        first.position_along_route_m <= second.position_along_route_m
        for first, second in zip(analysis.gates, analysis.gates[1:])
    )
    assert analysis.toll_road_detected is expect_toll_evidence
    if not expect_toll_evidence:
        # The service must still avoid claiming a free result merely because
        # this sparse index found no positive toll evidence.
        assert analysis.gates == []
