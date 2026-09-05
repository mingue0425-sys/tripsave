import pytest
from fastapi.testclient import TestClient

from app import app
from backend.places import load_places
from backend.geo import haversine_distance_meters
from backend.models import Location


pytestmark = pytest.mark.integration
client = TestClient(app)


def place_locations() -> dict[str, Location]:
    return {
        place.name: Location(
            lat=place.lat,
            lng=place.lng,
            label=place.name,
            source="local_search",
        )
        for place in load_places()
    }


REPRESENTATIVE_ROUTES = [
    ("서울", "인천"),
    ("서울", "대전"),
    ("서울", "부산"),
    ("부산", "대구"),
    ("부산", "경주"),
    ("대전", "광주"),
    ("강릉", "서울"),
    ("제주", "서귀포"),
]


@pytest.mark.parametrize("origin_name,destination_name", REPRESENTATIVE_ROUTES)
def test_real_local_osrm_route_for_representative_korean_routes(
    origin_name: str, destination_name: str
) -> None:
    status = client.get("/api/routing/status")
    if status.status_code != 200:
        pytest.skip("local OSRM is not running")

    places = place_locations()
    origin = places[origin_name]
    destination = places[destination_name]
    response = client.post(
        "/api/routes",
        json={"origin": origin.model_dump(), "destination": destination.model_dump()},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    route = payload["route"]
    assert route["distance_m"] > haversine_distance_meters(origin, destination)
    assert route["duration_s"] > 0
    assert route["geometry"]["type"] == "LineString"
    assert len(route["geometry"]["coordinates"]) >= 2
