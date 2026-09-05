from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import app, toll_calculator
from backend.tolls.models import TollResponse, TollResult, TollVehicleClass


client = TestClient(app)


def request_payload() -> dict:
    return {
        "origin": {"lat": 36.0, "lng": 127.0},
        "destination": {"lat": 36.0, "lng": 127.1},
        "route": {
            "distance_m": 10_000.0,
            "duration_s": 600.0,
            "geometry": {
                "type": "LineString",
                "coordinates": [[127.0, 36.0], [127.1, 36.0]],
            },
            "route_id": "route-api-test",
        },
        "route_id": "route-api-test",
        "vehicle_class": "class_1",
    }


def test_toll_api_returns_canonical_result(monkeypatch) -> None:
    async def fake_calculate(request):
        return TollResponse(
            status="ok",
            toll=TollResult(
                status="ok",
                complete=True,
                vehicle_class=TollVehicleClass.CLASS_1,
                total_toll_krw=1_000,
                known_toll_krw=1_000,
                route_id="route-api-test",
                source_status="fresh",
                fetched_at=datetime.now(timezone.utc),
            ),
        )

    monkeypatch.setattr(toll_calculator, "calculate", fake_calculate)
    response = client.post("/api/tolls/calculate", json=request_payload())

    assert response.status_code == 200
    assert response.json()["toll"]["total_toll_krw"] == 1_000
    assert response.json()["toll"]["complete"] is True


def test_toll_api_rejects_route_id_mismatch() -> None:
    payload = request_payload()
    payload["route_id"] = "different-route"
    response = client.post("/api/tolls/calculate", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_toll_status_maps_index_state(monkeypatch) -> None:
    async def unavailable():
        return {
            "status": "unavailable",
            "engine": "osm+tollgate-index+official-web",
            "local_index": False,
        }

    monkeypatch.setattr(toll_calculator, "status", unavailable)
    response = client.get("/api/tolls/status")
    assert response.status_code == 503
    assert response.json()["local_index"] is False
