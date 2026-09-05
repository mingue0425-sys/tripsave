"""Deterministic identifiers for canonical route results."""

from __future__ import annotations

import hashlib
import json

from backend.models import Location
from backend.routing.models import RouteResult


def make_route_id(
    origin: Location,
    destination: Location,
    route: RouteResult,
) -> str:
    """Bind a route result to endpoints and geometry without storing it server-side."""

    payload = {
        "origin": {"lat": origin.lat, "lng": origin.lng},
        "destination": {"lat": destination.lat, "lng": destination.lng},
        "distance_m": route.distance_m,
        "duration_s": route.duration_s,
        "geometry": route.geometry.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "route-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
