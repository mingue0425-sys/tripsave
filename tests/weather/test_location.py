import asyncio

import httpx
import pytest

from backend.weather.location import KmaWebLocationResolver, KmaLocationError, _grid_coordinates


def test_grid_conversion_matches_public_kma_area_lookup_cell():
    assert _grid_coordinates(37.5665, 126.9780) == (60, 127)


def test_public_location_lookup_is_fixed_and_validated():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=UTF-8"},
            json=[
                {
                    "code": "1114055000",
                    "name": "서울특별시 중구 명동",
                    "shortName": "명동",
                    "x": "60",
                    "y": "127",
                    "lat": "37.557236",
                    "lon": "126.98789",
                }
            ],
            request=request,
        )

    resolver = KmaWebLocationResolver(transport=httpx.MockTransport(handler), request_interval_s=0)
    try:
        location = asyncio.run(resolver.resolve(37.5665, 126.9780))
    finally:
        asyncio.run(resolver.close())

    assert location.code == "1114055000"
    assert location.x == 60 and location.y == 127
    assert requests[0].url.host == "www.weather.go.kr"
    assert requests[0].url.path == "/w/rest/zone/find/dong.do"
    assert requests[0].url.params["lang"] == "kor"


@pytest.mark.parametrize("status", [403, 429])
def test_location_access_denial_is_not_retried(status):
    def handler(request):
        return httpx.Response(status, request=request)

    resolver = KmaWebLocationResolver(transport=httpx.MockTransport(handler), request_interval_s=0)
    try:
        with pytest.raises(KmaLocationError) as raised:
            asyncio.run(resolver.resolve(37.5665, 126.9780))
        assert raised.value.code == "KMA_WEB_ACCESS_DENIED"
    finally:
        asyncio.run(resolver.close())


def test_location_response_must_match_requested_grid():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=[
                {
                    "code": "1114055000",
                    "name": "wrong cell",
                    "x": "1",
                    "y": "2",
                    "lat": "37.5",
                    "lon": "126.9",
                }
            ],
            request=request,
        )

    resolver = KmaWebLocationResolver(transport=httpx.MockTransport(handler), request_interval_s=0)
    try:
        with pytest.raises(KmaLocationError) as raised:
            asyncio.run(resolver.resolve(37.5665, 126.9780))
        assert raised.value.code == "KMA_WEB_LOCATION_NOT_FOUND"
    finally:
        asyncio.run(resolver.close())
