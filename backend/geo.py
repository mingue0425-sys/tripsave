"""Small coordinate helpers shared by routing validation and sanity checks."""

from __future__ import annotations

import math

from backend.models import Location
from config import SOUTH_KOREA_ROUTING_BOUNDS


EARTH_RADIUS_METERS = 6_371_008.8


def is_within_south_korea_routing_bounds(location: Location) -> bool:
    """Apply a broad preflight guard around the South Korea OSRM extract."""

    min_lat, max_lat, min_lng, max_lng = SOUTH_KOREA_ROUTING_BOUNDS
    return (
        min_lat <= location.lat <= max_lat
        and min_lng <= location.lng <= max_lng
    )


def haversine_distance_meters(first: Location, second: Location) -> float:
    """Return the great-circle distance between two validated locations."""

    delta_lat = math.radians(second.lat - first.lat)
    delta_lng = math.radians(second.lng - first.lng)
    first_lat = math.radians(first.lat)
    second_lat = math.radians(second.lat)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(first_lat)
        * math.cos(second_lat)
        * math.sin(delta_lng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(min(1.0, haversine)))
