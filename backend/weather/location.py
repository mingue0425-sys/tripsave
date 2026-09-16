"""KMA grid and public area-code resolution for weather requests."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from backend.async_lock import LoopLocalAsyncLock
from config import (
    WEATHER_KMA_WEB_ALLOWED_HOSTS,
    WEATHER_KMA_WEB_ALLOWED_PATHS,
    WEATHER_KMA_WEB_CONNECT_TIMEOUT_S,
    WEATHER_KMA_WEB_HTTP_TIMEOUT_S,
    WEATHER_KMA_WEB_LOCATION_URL,
    WEATHER_KMA_WEB_MAX_RESPONSE_BYTES,
    WEATHER_KMA_WEB_REQUEST_INTERVAL_S,
    WEATHER_KMA_WEB_USER_AGENT,
)


class KmaLocationError(RuntimeError):
    """Failure while resolving a coordinate through the public KMA flow."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class KmaLocation:
    code: str
    name: str
    x: int
    y: int
    lat: float
    lng: float


def _grid_coordinates(lat: float, lng: float) -> tuple[int, int]:
    """Convert WGS84 coordinates to the KMA Lambert grid.

    The official 날씨누리 JavaScript uses ``floor(value + 0.5)``.  Keeping
    that exact rounding rule in a small module prevents API and web paths
    from silently selecting different forecast cells at a boundary.
    """

    re = 6_371.00877
    grid = 5.0
    slat1, slat2 = 30.0, 60.0
    olon, olat = 126.0, 38.0
    xo, yo = 43, 136
    deg_to_rad = math.pi / 180.0
    re /= grid
    slat1 *= deg_to_rad
    slat2 *= deg_to_rad
    olon *= deg_to_rad
    olat *= deg_to_rad
    lat_rad = lat * deg_to_rad
    lng_rad = lng * deg_to_rad
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5)
        / math.tan(math.pi * 0.25 + slat1 * 0.5)
    )
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5) ** sn * math.cos(slat1) / sn
    ro = re * sf / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    ra = re * sf / math.tan(math.pi * 0.25 + lat_rad * 0.5) ** sn
    theta = lng_rad - olon
    if theta > math.pi:
        theta -= 2 * math.pi
    if theta < -math.pi:
        theta += 2 * math.pi
    theta *= sn
    return (
        math.floor(ra * math.sin(theta) + xo + 0.5),
        math.floor(ro - ra * math.cos(theta) + yo + 0.5),
    )


def _validate_kma_parts(
    value: str,
    *,
    expected_paths: set[str] | frozenset[str],
    allow_query: bool,
) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("KMA source URL has an invalid port") from error
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in WEATHER_KMA_WEB_ALLOWED_HOSTS
        or port not in {None, 443}
        or parsed.path not in expected_paths
        or parsed.username
        or parsed.password
        or (not allow_query and parsed.query)
        or parsed.fragment
    ):
        raise ValueError("KMA source URL must be an approved public KMA HTTPS page")
    return value


def validate_kma_url(value: str, *, allowed_paths: set[str] | frozenset[str] | None = None) -> str:
    """Validate a server-owned HTTPS KMA URL."""

    return _validate_kma_parts(
        value,
        expected_paths=allowed_paths or WEATHER_KMA_WEB_ALLOWED_PATHS,
        allow_query=False,
    )


def validate_kma_redirect_url(
    value: str,
    *,
    base_url: str,
    expected_path: str,
) -> str:
    """Validate a redirect target before any follow-up request is made."""

    target = urljoin(base_url, value)
    try:
        return _validate_kma_parts(
            target,
            expected_paths=frozenset({expected_path}),
            allow_query=True,
        )
    except ValueError as error:
        raise KmaLocationError("KMA_WEB_REDIRECT_REJECTED") from error


def validate_kma_response_url(response: httpx.Response, *, expected_path: str) -> None:
    """Reject redirects outside the KMA host/path allowlist."""

    parsed = urlsplit(str(response.url))
    try:
        port = parsed.port
    except ValueError as error:
        raise KmaLocationError("KMA_WEB_REDIRECT_REJECTED") from error
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in WEATHER_KMA_WEB_ALLOWED_HOSTS
        or port not in {None, 443}
        or parsed.path != expected_path
        or parsed.username
        or parsed.password
    ):
        raise KmaLocationError(
            "KMA_WEB_REDIRECT_REJECTED",
            "KMA public request redirected outside the approved host/path",
        )


class KmaWebLocationResolver:
    """Resolve lat/lng using the public page's normal area lookup request."""

    def __init__(
        self,
        *,
        source_url: str = WEATHER_KMA_WEB_LOCATION_URL,
        timeout_s: float = WEATHER_KMA_WEB_HTTP_TIMEOUT_S,
        connect_timeout_s: float = WEATHER_KMA_WEB_CONNECT_TIMEOUT_S,
        cache_ttl_s: float = 86_400.0,
        request_interval_s: float = WEATHER_KMA_WEB_REQUEST_INTERVAL_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.source_url = validate_kma_url(
            source_url,
            allowed_paths=frozenset({"/w/rest/zone/find/dong.do"}),
        )
        self.timeout = httpx.Timeout(
            max(0.1, timeout_s),
            connect=max(0.1, min(timeout_s, connect_timeout_s)),
        )
        self.cache_ttl_s = max(1.0, float(cache_ttl_s))
        self.request_interval_s = max(0.0, float(request_interval_s))
        self.transport = transport
        self._client: httpx.AsyncClient | None = None
        self._client_lock = LoopLocalAsyncLock()
        self._lookup_lock = LoopLocalAsyncLock()
        self._next_request_at = 0.0
        self._cache: dict[tuple[float, float], tuple[KmaLocation, datetime]] = {}
        self.last_diagnostics: dict[str, object] = {}

    async def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                headers={
                    "User-Agent": WEATHER_KMA_WEB_USER_AGENT,
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": "ko-KR,ko;q=0.9",
                },
                transport=self.transport,
            )
        return self._client

    @staticmethod
    def _cache_key(lat: float, lng: float) -> tuple[float, float]:
        return round(lat, 5), round(lng, 5)

    async def resolve(self, lat: float, lng: float) -> KmaLocation:
        if (
            not math.isfinite(lat)
            or not math.isfinite(lng)
            or not -90.0 <= lat <= 90.0
            or not -180.0 <= lng <= 180.0
            or abs(lat) >= 90.0
        ):
            raise KmaLocationError("KMA_WEB_LOCATION_NOT_FOUND")
        key = self._cache_key(lat, lng)
        now = datetime.now(timezone.utc)
        cached = self._cache.get(key)
        if cached is not None and now - cached[1] <= timedelta(seconds=self.cache_ttl_s):
            self.last_diagnostics = {
                "provider": "kma_web",
                "location_url": self.source_url,
                "location_cache": "hit",
                "location_code": cached[0].code,
            }
            return cached[0]

        async with self._lookup_lock:
            # A concurrent caller may have filled the cache while this caller
            # was waiting for the per-loop lock.
            now = datetime.now(timezone.utc)
            cached = self._cache.get(key)
            if cached is not None and now - cached[1] <= timedelta(seconds=self.cache_ttl_s):
                return cached[0]
            x, y = _grid_coordinates(lat, lng)
            params = {
                "x": x,
                "y": y,
                "lat": f"{lat:.6f}",
                "lon": f"{lng:.6f}",
                "lang": "kor",
            }
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = time.monotonic() + self.request_interval_s
            try:
                client = await self._client_or_create()
                request_url = self.source_url
                request_params: dict[str, object] | None = params
                for _ in range(3):
                    response = await client.get(request_url, params=request_params)
                    if 300 <= response.status_code < 400:
                        redirect = response.headers.get("location")
                        if not redirect:
                            raise KmaLocationError("KMA_WEB_REDIRECT_REJECTED")
                        request_url = validate_kma_redirect_url(
                            redirect,
                            base_url=str(response.url),
                            expected_path="/w/rest/zone/find/dong.do",
                        )
                        request_params = None
                        continue
                    break
                else:
                    raise KmaLocationError("KMA_WEB_REDIRECT_REJECTED")
            except (httpx.TimeoutException, TimeoutError) as error:
                raise KmaLocationError("KMA_WEB_TIMEOUT") from error
            except httpx.HTTPError as error:
                raise KmaLocationError("KMA_WEB_SOURCE_FAILED") from error

            try:
                validate_kma_response_url(
                    response,
                    expected_path="/w/rest/zone/find/dong.do",
                )
            except KmaLocationError:
                raise
            if response.status_code in {401, 403, 429}:
                raise KmaLocationError("KMA_WEB_ACCESS_DENIED")
            if response.status_code >= 400:
                raise KmaLocationError("KMA_WEB_SOURCE_FAILED")
            if len(response.content) > WEATHER_KMA_WEB_MAX_RESPONSE_BYTES:
                raise KmaLocationError("KMA_WEB_PAGE_CHANGED")
            content_type = response.headers.get("content-type", "").casefold()
            if "json" not in content_type:
                raise KmaLocationError("KMA_WEB_PAGE_CHANGED")
            try:
                payload = response.json()
            except ValueError as error:
                raise KmaLocationError("KMA_WEB_PARSE_FAILED") from error
            if not isinstance(payload, list) or not payload:
                raise KmaLocationError("KMA_WEB_LOCATION_NOT_FOUND")

            item = next(
                (
                    candidate
                    for candidate in payload
                    if isinstance(candidate, dict)
                    and isinstance(candidate.get("code"), str)
                    and candidate["code"].isdigit()
                    and len(candidate["code"]) == 10
                ),
                None,
            )
            if item is None:
                raise KmaLocationError("KMA_WEB_LOCATION_NOT_FOUND")
            try:
                location = KmaLocation(
                    code=str(item["code"]),
                    name=str(item.get("name") or item.get("shortName") or "KMA area"),
                    x=int(item["x"]),
                    y=int(item["y"]),
                    lat=float(item["lat"]),
                    lng=float(item["lon"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise KmaLocationError("KMA_WEB_LOCATION_NOT_FOUND") from error
            if not all(
                math.isfinite(value)
                for value in (location.lat, location.lng)
            ) or not -90.0 <= location.lat <= 90.0 or not -180.0 <= location.lng <= 180.0:
                raise KmaLocationError("KMA_WEB_LOCATION_NOT_FOUND")
            if len(location.name) > 200:
                raise KmaLocationError("KMA_WEB_LOCATION_NOT_FOUND")
            # The public endpoint occasionally returns the adjacent official
            # cell for a coordinate near a grid boundary (the normal KMA UI
            # accepts that response too).  Keep the check tight enough to
            # reject a silent unrelated area while allowing that documented
            # one-cell normalization.
            if abs(location.x - x) > 1 or abs(location.y - y) > 1:
                raise KmaLocationError("KMA_WEB_LOCATION_NOT_FOUND")
            self._cache[key] = (location, now)
            self.last_diagnostics = {
                "provider": "kma_web",
                "location_url": self.source_url,
                "location_cache": "miss",
                "location_http_status": response.status_code,
                "location_code": location.code,
                "location_name": location.name,
                "grid": {"x": x, "y": y},
            }
            return location

    async def close(self) -> None:
        async with self._client_lock:
            client, self._client = self._client, None
            if client is not None:
                await client.aclose()


__all__ = [
    "KmaLocation",
    "KmaLocationError",
    "KmaWebLocationResolver",
    "_grid_coordinates",
    "validate_kma_response_url",
    "validate_kma_url",
]
