import pytest
from pydantic import ValidationError

from backend.models import Location
from backend.routing.models import RouteGeometry, RouteRequest
from backend.routing.osrm import build_route_url, location_to_osrm_coordinate


def location(lat: float = 35.1796, lng: float = 129.0756) -> Location:
    return Location(lat=lat, lng=lng, source="map")


def test_osrm_coordinate_order_is_longitude_then_latitude() -> None:
    assert location_to_osrm_coordinate(location()) == "129.0756,35.1796"


def test_route_url_has_local_host_and_driving_profile() -> None:
    url = build_route_url(
        "http://127.0.0.1:5000",
        location(),
        location(lat=37.5665, lng=126.978),
    )

    assert url == (
        "http://127.0.0.1:5000/route/v1/car/"
        "129.0756,35.1796;126.978,37.5665"
    )


def test_external_osrm_url_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_route_url(
            "https://router.project-osrm.org",
            location(),
            location(lat=37.5665, lng=126.978),
        )


@pytest.mark.parametrize(
    "origin,destination",
    [
        (Location(lat=40.0, lng=127.0), location()),
        (location(), Location(lat=35.0, lng=140.0)),
    ],
)
def test_route_request_rejects_coordinates_outside_south_korea_dataset(
    origin: Location, destination: Location
) -> None:
    with pytest.raises(ValidationError, match="South Korea routing dataset"):
        RouteRequest(origin=origin, destination=destination)


@pytest.mark.parametrize(
    "origin,destination",
    [
        (location(), location()),
        (location(), location(lat=35.1797, lng=129.0756)),
    ],
)
def test_route_request_rejects_same_or_nearby_locations(origin, destination) -> None:
    with pytest.raises(ValidationError):
        RouteRequest(origin=origin, destination=destination)


@pytest.mark.parametrize(
    "coordinates",
    [
        [[129.0, 35.0]],
        [[181.0, 35.0], [129.0, 35.0]],
        [[129.0, 91.0], [129.1, 35.1]],
        [["129.0", 35.0], [129.1, 35.1]],
        [[129.0, 35.0, 1], [129.1, 35.1, 1]],
    ],
)
def test_route_geometry_rejects_invalid_geojson_coordinates(coordinates) -> None:
    with pytest.raises(ValidationError):
        RouteGeometry(type="LineString", coordinates=coordinates)
