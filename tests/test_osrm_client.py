import asyncio

import httpx
import pytest

from backend.models import Location
from backend.routing.errors import (
    InvalidRouteInputError,
    InvalidRouteResponseError,
    NoRouteError,
    NoSegmentError,
    RoutingEngineUnavailableError,
    RoutingTimeoutError,
)
from backend.routing.osrm import OSRMClient, parse_osrm_route_payload


SEOUL = Location(lat=37.5665, lng=126.978, source="map")
BUSAN = Location(lat=35.1796, lng=129.0756, source="map")


def valid_payload() -> dict:
    return {
        "code": "Ok",
        "waypoints": [
            {"location": [126.978, 37.5665]},
            {"location": [129.0756, 35.1796]},
        ],
        "routes": [
            {
                "distance": 392636.3,
                "duration": 17467.2,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [126.978, 37.5665],
                        [128.0, 36.5],
                        [129.0756, 35.1796],
                    ],
                },
            }
        ],
    }


def test_osrm_payload_becomes_canonical_route_result() -> None:
    result = parse_osrm_route_payload(valid_payload(), SEOUL, BUSAN)

    assert result.distance_m == 392636.3
    assert result.duration_s == 17467.2
    assert result.geometry.type == "LineString"
    assert result.geometry.coordinates[0] == [126.978, 37.5665]


@pytest.mark.parametrize("code,error_type", [("NoRoute", NoRouteError), ("NoSegment", NoSegmentError)])
def test_osrm_no_route_codes_are_typed(code, error_type) -> None:
    with pytest.raises(error_type):
        parse_osrm_route_payload({"code": code}, SEOUL, BUSAN)


def test_osrm_empty_routes_are_not_success() -> None:
    payload = valid_payload()
    payload["routes"] = []

    with pytest.raises(NoRouteError):
        parse_osrm_route_payload(payload, SEOUL, BUSAN)


def test_osrm_malformed_geometry_is_rejected() -> None:
    payload = valid_payload()
    payload["routes"][0]["geometry"] = {"type": "Point", "coordinates": []}

    with pytest.raises(InvalidRouteResponseError):
        parse_osrm_route_payload(payload, SEOUL, BUSAN)


def test_osrm_far_snap_is_rejected() -> None:
    payload = valid_payload()
    payload["waypoints"][0]["location"] = [127.5, 35.0]

    with pytest.raises(InvalidRouteInputError):
        parse_osrm_route_payload(payload, SEOUL, BUSAN)


class FakeAsyncClient:
    response = None
    failure = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        if self.failure is not None:
            raise self.failure
        return self.response


def run_client_route() -> object:
    return asyncio.run(OSRMClient(base_url="http://127.0.0.1:5000").route(SEOUL, BUSAN))


def test_osrm_client_maps_timeout(monkeypatch) -> None:
    FakeAsyncClient.failure = httpx.ReadTimeout("timed out")
    FakeAsyncClient.response = None
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(RoutingTimeoutError):
        run_client_route()


def test_osrm_client_maps_connection_refused(monkeypatch) -> None:
    FakeAsyncClient.failure = httpx.ConnectError("connection refused")
    FakeAsyncClient.response = None
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(RoutingEngineUnavailableError):
        run_client_route()


def test_osrm_client_rejects_malformed_json(monkeypatch) -> None:
    class MalformedResponse:
        status_code = 200

        @staticmethod
        def json():
            raise ValueError("not json")

    FakeAsyncClient.failure = None
    FakeAsyncClient.response = MalformedResponse()
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(InvalidRouteResponseError):
        run_client_route()


def test_osrm_client_maps_non_json_server_error_to_unavailable(monkeypatch) -> None:
    class ServerErrorResponse:
        status_code = 500

        @staticmethod
        def json():
            raise ValueError("not json")

    FakeAsyncClient.failure = None
    FakeAsyncClient.response = ServerErrorResponse()
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(RoutingEngineUnavailableError):
        run_client_route()
