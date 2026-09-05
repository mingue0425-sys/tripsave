import math

import pytest
from pydantic import ValidationError

from backend.models import Location


@pytest.mark.parametrize(
    "payload",
    [
        {"lat": 91.0, "lng": 129.0},
        {"lat": -91.0, "lng": 129.0},
        {"lat": 35.0, "lng": 181.0},
        {"lat": 35.0, "lng": -181.0},
        {"lat": math.nan, "lng": 129.0},
        {"lat": math.inf, "lng": 129.0},
        {"lat": "35.1796", "lng": 129.0756},
    ],
)
def test_location_rejects_invalid_coordinates(payload) -> None:
    with pytest.raises(ValidationError):
        Location(**payload)


def test_location_uses_explicit_lat_lng_schema() -> None:
    location = Location(
        lat=35.1796,
        lng=129.0756,
        label="부산",
        source="local_search",
    )

    assert location.model_dump() == {
        "lat": 35.1796,
        "lng": 129.0756,
        "label": "부산",
        "source": "local_search",
    }
