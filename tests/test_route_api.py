from fastapi.testclient import TestClient

from app import app, routing_client
from backend.routing.errors import (
    NoRouteError,
    NoSegmentError,
    RoutingEngineUnavailableError,
    RoutingTimeoutError,
)
from backend.routing.models import RouteGeometry, RouteResult


client = TestClient(app)


def request_payload() -> dict:
    return {
        "origin": {"lat": 37.5665, "lng": 126.978},
        "destination": {"lat": 35.1796, "lng": 129.0756},
    }


def canonical_route() -> RouteResult:
    return RouteResult(
        distance_m=392636.3,
        duration_s=17467.2,
        geometry=RouteGeometry(
            type="LineString",
            coordinates=[
                [126.978, 37.5665],
                [128.0, 36.5],
                [129.0756, 35.1796],
            ],
        ),
    )


def test_route_api_returns_canonical_result_without_osrm_raw_shape(monkeypatch) -> None:
    async def fake_route(origin, destination):
        return canonical_route()

    monkeypatch.setattr(routing_client, "route", fake_route)
    response = client.post("/api/routes", json=request_payload())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "route": {
            "distance_m": 392636.3,
            "duration_s": 17467.2,
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [126.978, 37.5665],
                    [128.0, 36.5],
                    [129.0756, 35.1796],
                ],
            },
        },
    }


def test_route_api_rejects_invalid_coordinates() -> None:
    payload = request_payload()
    payload["origin"]["lat"] = 91

    response = client.post("/api/routes", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_route_api_maps_engine_unavailable(monkeypatch) -> None:
    async def unavailable(origin, destination):
        raise RoutingEngineUnavailableError()

    monkeypatch.setattr(routing_client, "route", unavailable)
    response = client.post("/api/routes", json=request_payload())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ROUTING_ENGINE_UNAVAILABLE"


def test_route_api_maps_no_route(monkeypatch) -> None:
    async def no_route(origin, destination):
        raise NoRouteError()

    monkeypatch.setattr(routing_client, "route", no_route)
    response = client.post("/api/routes", json=request_payload())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ROUTE_NOT_FOUND"


def test_route_api_maps_no_segment(monkeypatch) -> None:
    async def no_segment(origin, destination):
        raise NoSegmentError()

    monkeypatch.setattr(routing_client, "route", no_segment)
    response = client.post("/api/routes", json=request_payload())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NO_SEGMENT"


def test_route_api_maps_timeout(monkeypatch) -> None:
    async def timeout(origin, destination):
        raise RoutingTimeoutError()

    monkeypatch.setattr(routing_client, "route", timeout)
    response = client.post("/api/routes", json=request_payload())

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "ROUTING_TIMEOUT"


def test_routing_status_maps_ready_and_unavailable(monkeypatch) -> None:
    async def ready():
        return {"status": "ready", "engine": "osrm", "profile": "car", "local": True}

    monkeypatch.setattr(routing_client, "status", ready)
    ready_response = client.get("/api/routing/status")
    assert ready_response.status_code == 200

    async def unavailable():
        return {
            "status": "unavailable",
            "engine": "osrm",
            "profile": "car",
            "local": True,
        }

    monkeypatch.setattr(routing_client, "status", unavailable)
    unavailable_response = client.get("/api/routing/status")
    assert unavailable_response.status_code == 503
