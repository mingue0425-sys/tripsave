from fastapi.testclient import TestClient

from app import app, poi_service

client = TestClient(app)


def test_poi_api_reports_missing_local_index_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        poi_service.repository,
        "database_path",
        tmp_path / "missing-poi.sqlite3",
    )
    response = client.post(
        "/api/poi/search",
        json={
            "destination": {"lat": 35.1796, "lng": 129.0756},
            "categories": ["hospital", "pharmacy"],
        },
    )

    assert response.status_code == 200
    assert response.json()["complete"] is False
    assert response.json()["category_statuses"] == {
        "hospital": "unavailable",
        "pharmacy": "unavailable",
    }
    assert response.json()["results"] == []


def test_itinerary_api_uses_supplied_matrix_without_osrm():
    response = client.post(
        "/api/routes/optimize",
        json={
            "origin": {"lat": 35.10, "lng": 129.00, "name": "출발"},
            "destination": {"lat": 35.20, "lng": 129.10, "name": "도착"},
            "waypoints": [
                {"id": "poi-1", "lat": 35.15, "lng": 129.05, "name": "경유지", "visit_duration_min": 30}
            ],
            "mode": "fastest",
            "start_datetime": "2026-10-03T09:00:00",
            "include_geometry": False,
            "matrix": [
                {"from_id": "origin", "to_id": "poi-1", "distance_m": 1000, "duration_s": 1800},
                {"from_id": "poi-1", "to_id": "destination", "distance_m": 1200, "duration_s": 1800},
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["feasible"] is True
    assert [item["id"] for item in payload["ordered_waypoints"]] == ["poi-1"]
    assert payload["total_distance_m"] == 2200
    assert payload["segments"][0]["arrival_time"] == "2026-10-03T09:30:00+09:00"
    assert payload["segments"][0]["next_departure_time"] == "2026-10-03T10:00:00+09:00"
    assert payload["total_cost_krw"] is None


def test_itinerary_api_lowest_cost_does_not_fall_back_to_duration():
    response = client.post(
        "/api/routes/optimize",
        json={
            "origin": {"lat": 35.10, "lng": 129.00, "name": "출발"},
            "destination": {"lat": 35.20, "lng": 129.10, "name": "도착"},
            "waypoints": [],
            "mode": "lowest_cost",
            "include_geometry": False,
            "matrix": [
                {
                    "from_id": "origin",
                    "to_id": "destination",
                    "distance_m": 1000,
                    "duration_s": 60,
                },
                {
                    "from_id": "destination",
                    "to_id": "origin",
                    "distance_m": 1000,
                    "duration_s": 60,
                },
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["feasible"] is False
    assert response.json()["warnings"] == ["ROUTE_COST_MATRIX_INCOMPLETE"]


def test_weather_api_preserves_horizon_blocker():
    response = client.post(
        "/api/weather/forecast",
        json={
            "lat": 35.1796,
            "lng": 129.0756,
            "start_date": "2099-10-03",
            "end_date": "2099-10-03",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "not_available_yet"
    assert response.json()["forecast"][0]["precipitation_mm"] is None
