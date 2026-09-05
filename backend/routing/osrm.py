"""HTTP client and response validation for the local OSRM server."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from backend.geo import haversine_distance_meters
from backend.models import Location
from config import (
    OSRM_BASE_URL,
    OSRM_CONNECT_TIMEOUT_S,
    OSRM_MAX_SNAP_DISTANCE_M,
    OSRM_PORT,
    OSRM_PROFILE,
    OSRM_REQUEST_TIMEOUT_S,
    validate_local_osrm_base_url,
)
from .errors import (
    InvalidRouteInputError,
    InvalidRouteResponseError,
    NoRouteError,
    NoSegmentError,
    RoutingEngineUnavailableError,
    RoutingTimeoutError,
)
from .identity import make_route_id
from .models import RouteResult


def location_to_osrm_coordinate(location: Location) -> str:
    """Convert canonical lat/lng to OSRM's required ``lng,lat`` text."""

    return f"{location.lng:.15g},{location.lat:.15g}"


def build_route_url(
    base_url: str,
    origin: Location,
    destination: Location,
) -> str:
    """Build a local OSRM route URL without accepting a client-supplied host."""

    safe_base_url = validate_local_osrm_base_url(base_url)
    coordinates = ";".join(
        [
            location_to_osrm_coordinate(origin),
            location_to_osrm_coordinate(destination),
        ]
    )
    encoded_coordinates = quote(coordinates, safe=",;.-")
    return f"{safe_base_url}/route/v1/{OSRM_PROFILE}/{encoded_coordinates}"


def _number(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
    ):
        raise InvalidRouteResponseError(f"OSRM field '{field_name}' is not finite.")
    return float(value)


def _waypoint_location(value: object, index: int) -> Location:
    if not isinstance(value, dict):
        raise InvalidRouteResponseError("OSRM waypoint data is malformed.")
    raw_location = value.get("location")
    if (
        not isinstance(raw_location, list)
        or len(raw_location) != 2
        or any(
            isinstance(item, bool)
            or not isinstance(item, Real)
            or not math.isfinite(float(item))
            for item in raw_location
        )
    ):
        raise InvalidRouteResponseError(
            f"OSRM waypoint {index} has no valid [lng, lat] location."
        )
    longitude, latitude = (float(item) for item in raw_location)
    try:
        return Location(lat=latitude, lng=longitude, source="osrm")
    except ValidationError as error:
        raise InvalidRouteResponseError(
            f"OSRM waypoint {index} is outside coordinate bounds."
        ) from error


def parse_osrm_route_payload(
    payload: object,
    origin: Location,
    destination: Location,
    max_snap_distance_m: float = OSRM_MAX_SNAP_DISTANCE_M,
) -> RouteResult:
    """Validate OSRM JSON and convert it to the application route schema."""

    if not isinstance(payload, dict):
        raise InvalidRouteResponseError("OSRM response is not a JSON object.")

    code = payload.get("code")
    if code == "NoRoute":
        raise NoRouteError()
    if code == "NoSegment":
        raise NoSegmentError()
    if code != "Ok":
        raise InvalidRouteResponseError(f"OSRM returned unexpected code: {code!r}.")

    raw_routes = payload.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise NoRouteError()
    raw_route = raw_routes[0]
    if not isinstance(raw_route, dict):
        raise InvalidRouteResponseError("OSRM route object is malformed.")

    try:
        route = RouteResult.model_validate(
            {
                "distance_m": _number(raw_route.get("distance"), "distance"),
                "duration_s": _number(raw_route.get("duration"), "duration"),
                "geometry": raw_route.get("geometry"),
            }
        )
    except (ValidationError, InvalidRouteResponseError) as error:
        if isinstance(error, InvalidRouteResponseError):
            raise
        raise InvalidRouteResponseError() from error

    raw_waypoints = payload.get("waypoints")
    if not isinstance(raw_waypoints, list) or len(raw_waypoints) < 2:
        raise InvalidRouteResponseError("OSRM response has insufficient waypoints.")
    snapped_origin = _waypoint_location(raw_waypoints[0], 0)
    snapped_destination = _waypoint_location(raw_waypoints[-1], len(raw_waypoints) - 1)
    if (
        haversine_distance_meters(origin, snapped_origin) > max_snap_distance_m
        or haversine_distance_meters(destination, snapped_destination)
        > max_snap_distance_m
    ):
        raise InvalidRouteInputError(
            "선택한 위치가 가장 가까운 자동차 도로에서 5km 이상 떨어져 있습니다."
        )

    straight_distance = haversine_distance_meters(origin, destination)
    tolerance = max(100.0, straight_distance * 0.001)
    if route.distance_m + tolerance < straight_distance:
        raise InvalidRouteResponseError(
            "OSRM route distance is shorter than the straight-line distance."
        )
    return route


class OSRMClient:
    """Small async client restricted to the configured local OSRM server."""

    def __init__(
        self,
        base_url: str = OSRM_BASE_URL,
        request_timeout_s: float = OSRM_REQUEST_TIMEOUT_S,
        connect_timeout_s: float = OSRM_CONNECT_TIMEOUT_S,
    ) -> None:
        self.base_url = validate_local_osrm_base_url(base_url)
        self.timeout = httpx.Timeout(
            request_timeout_s,
            connect=connect_timeout_s,
        )

    async def route(self, origin: Location, destination: Location) -> RouteResult:
        url = build_route_url(self.base_url, origin, destination)
        params = {
            "overview": "full",
            "geometries": "geojson",
            "steps": "false",
            "alternatives": "false",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException as error:
            raise RoutingTimeoutError() from error
        except httpx.RequestError as error:
            raise RoutingEngineUnavailableError() from error

        if response.status_code >= 500:
            raise RoutingEngineUnavailableError(
                "로컬 경로 엔진이 서버 오류를 반환했습니다."
            )
        try:
            payload: Any = response.json()
        except ValueError as error:
            raise InvalidRouteResponseError() from error
        route = parse_osrm_route_payload(payload, origin, destination)
        return route.model_copy(update={"route_id": make_route_id(origin, destination, route)})

    async def status(self) -> dict[str, object]:
        """Probe a known local road without exposing the OSRM port to browsers."""

        probe = Location(lat=37.5665, lng=126.978, source="status_probe")
        url = (
            f"{self.base_url}/nearest/v1/{OSRM_PROFILE}/"
            f"{location_to_osrm_coordinate(probe)}"
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params={"number": 1})
        except httpx.TimeoutException:
            return self._unavailable("Local OSRM health probe timed out.")
        except httpx.RequestError:
            return self._unavailable()

        if response.status_code >= 500:
            return self._unavailable("Local OSRM returned a server error.")
        try:
            payload = response.json()
        except ValueError:
            return self._unavailable("Local OSRM returned invalid health data.")
        if response.status_code >= 400 or not isinstance(payload, dict):
            return self._unavailable()
        if payload.get("code") != "Ok":
            return self._unavailable("Local OSRM could not snap its health probe.")
        return {
            "status": "ready",
            "engine": "osrm",
            "profile": OSRM_PROFILE,
            "local": True,
            "port": OSRM_PORT,
        }

    @staticmethod
    def _unavailable(message: str = "Local OSRM is not running.") -> dict[str, object]:
        return {
            "status": "unavailable",
            "engine": "osrm",
            "profile": OSRM_PROFILE,
            "local": True,
            "port": OSRM_PORT,
            "message": message,
        }
