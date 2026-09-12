import pytest
from fastapi.testclient import TestClient

from app import app


pytestmark = [pytest.mark.integration, pytest.mark.official]

PLACES = {
    "서울": {"lat": 37.5665, "lng": 126.978},
    "부산": {"lat": 35.1796, "lng": 129.0756},
    "대구": {"lat": 35.8714, "lng": 128.6014},
    "강릉": {"lat": 37.7519, "lng": 128.8761},
    "대전": {"lat": 36.3504, "lng": 127.3845},
    "광주": {"lat": 35.1595, "lng": 126.8514},
}

PAIRS = [
    ("서울", "부산"),
    ("서울", "대전"),
    ("부산", "대구"),
    ("강릉", "서울"),
    ("대전", "광주"),
]


def location(name: str) -> dict[str, object]:
    return {**PLACES[name], "label": name, "source": "local_search"}


@pytest.mark.parametrize("origin_name,destination_name", PAIRS)
def test_live_representative_route_has_real_toll_fuel_and_cost(
    origin_name: str, destination_name: str
) -> None:
    with TestClient(app) as client:
        route_response = client.post(
            "/api/routes",
            json={"origin": location(origin_name), "destination": location(destination_name)},
        )
        assert route_response.status_code == 200
        route = route_response.json()["route"]

        cost_response = client.post(
            "/api/costs/driving",
            json={
                "route_id": route["route_id"],
                "origin": location(origin_name),
                "destination": location(destination_name),
                "route": route,
                "fuel_type": "gasoline",
                "fuel_efficiency_km_per_l": 13.5,
                "vehicle_class": "class_1",
                "round_trip_mode": "directional",
            },
        )

    assert cost_response.status_code == 200
    body = cost_response.json()
    assert body["status"] == "ok"
    assert body["route"]["distance_m"] == route["distance_m"]
    assert body["toll"]["complete"] is True
    assert isinstance(body["toll"]["total_toll_krw"], int)
    assert body["fuel"]["complete"] is True
    assert body["fuel"]["price_krw_per_l"] > 0
    assert body["fuel"]["fuel_volume_l"] > 0
    driving = body["driving_cost"]
    assert driving["cost_complete"] is True
    assert driving["outbound"]["complete"] is True
    assert driving["return"]["complete"] is True
    assert isinstance(driving["outbound"]["total_krw"], int)
    assert isinstance(driving["return"]["total_krw"], int)
    assert driving["round_trip"]["complete"] is True
    assert isinstance(driving["round_trip"]["total_krw"], int)
    assert body["return_route"]["distance_m"] == driving["return"]["distance_m"]
    assert driving["round_trip"]["distance_m"] == (
        driving["outbound"]["distance_m"] + driving["return"]["distance_m"]
    )
    assert driving["round_trip"]["fuel_volume_l"] == (
        driving["outbound"]["fuel_volume_l"] + driving["return"]["fuel_volume_l"]
    )
    assert driving["round_trip"]["fuel_cost_krw"] == (
        driving["outbound"]["fuel_cost_krw"] + driving["return"]["fuel_cost_krw"]
    )
    assert driving["round_trip"]["toll_krw"] == (
        driving["outbound"]["toll_krw"] + driving["return"]["toll_krw"]
    )
    assert driving["round_trip"]["total_krw"] == (
        driving["outbound"]["total_krw"] + driving["return"]["total_krw"]
    )
    assert driving["round_trip"]["total_krw"] >= driving["outbound"]["total_krw"]
    assert driving["return"]["fuel_cost_krw"] > 0
    assert driving["round_trip"]["fuel_cost_krw"] > driving["outbound"]["fuel_cost_krw"]
    assert driving["round_trip_toll"]["mode"] in {
        "verified_official",
        "estimated_doubled_outbound",
    }
    if driving["round_trip_toll"]["mode"] == "estimated_doubled_outbound":
        assert driving["round_trip_toll"]["verified"] is False
        assert driving["officially_verified"] is False
