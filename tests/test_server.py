from fastapi.testclient import TestClient

from app import app
from config import APP_VERSION

client = TestClient(app)


def test_root_renders_open_basemap_shell() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Korea Trip Optimizer" in response.text
    assert "/static/vendor/maplibre-gl/maplibre-gl.js" in response.text
    assert "tiles.openfreemap.org/styles/liberty" in response.text
    assert "/static/js/selection.js" in response.text
    assert "/static/js/markers.js" in response.text
    assert "/static/js/route.js" in response.text
    assert "/static/js/toll.js" in response.text
    assert "/static/js/cost.js" in response.text
    assert "/static/js/toll_markers.js" in response.text
    assert "/static/css/trips.css" in response.text
    assert "/static/css/recommendations.css" in response.text
    assert "/static/js/trips.js" in response.text
    assert "/static/js/recommendations.js" in response.text
    assert "/static/css/poi.css" in response.text
    assert "/static/css/itinerary.css" in response.text
    assert "/static/css/weather.css" in response.text
    assert "/static/js/poi.js" in response.text
    assert "/static/js/itinerary.js" in response.text
    assert "/static/js/weather.js" in response.text
    assert "trip-candidates-panel" in response.text
    assert "recommendations-panel" in response.text
    assert "poi-panel" in response.text
    assert "itinerary-panel" in response.text
    assert "weather-panel" in response.text
    assert "/api/routes" in response.text
    assert "/api/tolls/calculate" in response.text
    assert "/api/fuel/calculate" in response.text
    assert "/api/costs/driving" in response.text
    assert "/api/trips/candidates" in response.text
    assert "/api/recommendations/rank" in response.text
    assert "/api/poi/search" in response.text
    assert "/api/routes/optimize" in response.text
    assert "/api/weather/forecast" in response.text
    assert "/api/places/search" in response.text
    assert "OpenFreeMap" in response.text


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": APP_VERSION}


def test_retired_preview_map_routes_are_not_served() -> None:
    assert client.get("/map-data/korea-basemap.geojson").status_code == 404
    assert client.get("/api/map/status").status_code == 404


def test_local_place_search_route() -> None:
    response = client.get("/api/places/search", params={"q": "Busan"})

    assert response.status_code == 200
    assert response.json()["query"] == "Busan"
    assert response.json()["results"][0]["id"] == "city-busan"
    assert response.json()["results"][0]["lat"] == 35.1796


def test_static_file_mount_does_not_expose_parent_paths() -> None:
    response = client.get("/static/../config.py")

    assert response.status_code in {404, 400}
