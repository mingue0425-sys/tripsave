from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.models import Location
from backend.routing.models import RouteGeometry, RouteResult
from backend.tolls.models import (
    TollCalculationRequest,
    TollGate,
    TollResult,
    TollVehicleClass,
)


def route() -> RouteResult:
    return RouteResult(
        distance_m=10_000.0,
        duration_s=600.0,
        geometry=RouteGeometry(
            type="LineString",
            coordinates=[[127.0, 36.0], [127.1, 36.0]],
        ),
        route_id="route-test",
    )


def test_vehicle_class_enum_covers_official_columns() -> None:
    assert [item.value for item in TollVehicleClass] == [
        "class_1",
        "class_2",
        "class_3",
        "class_4",
        "class_5",
        "compact",
    ]


def test_toll_request_keeps_explicit_location_objects_and_route_id() -> None:
    request = TollCalculationRequest(
        route_id="route-test",
        origin=Location(lat=36.0, lng=127.0),
        destination=Location(lat=36.0, lng=127.1),
        route=route(),
        vehicle_class=TollVehicleClass.COMPACT,
    )
    assert request.origin.lat == 36.0
    assert request.origin.lng == 127.0
    assert request.vehicle_class is TollVehicleClass.COMPACT


def test_toll_result_never_allows_unknown_total_as_zero() -> None:
    with pytest.raises(ValidationError):
        TollResult(
            status="partial",
            complete=False,
            vehicle_class=TollVehicleClass.CLASS_1,
            total_toll_krw=0,
            route_id="route-test",
        )


def test_complete_toll_result_requires_total() -> None:
    with pytest.raises(ValidationError):
        TollResult(
            status="ok",
            complete=True,
            vehicle_class=TollVehicleClass.CLASS_1,
            route_id="route-test",
            fetched_at=datetime.now(timezone.utc),
        )


def test_toll_gate_uses_explicit_lat_lng_fields() -> None:
    gate = TollGate(
        id="osm-node-1",
        osm_type="node",
        osm_id=1,
        name="서울요금소",
        normalized_name="서울",
        lat=37.0,
        lng=127.0,
        gate_type="toll_booth",
    )
    assert gate.lat == 37.0
    assert gate.lng == 127.0
