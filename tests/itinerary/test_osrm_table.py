import asyncio

import httpx
import pytest

from backend.models import Location
from backend.routing.errors import InvalidRouteResponseError, NoRouteError
from backend.routing.osrm import OSRMClient


class FakeAsyncClient:
    response = None

    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        del args

    async def get(self, *args, **kwargs):
        del args, kwargs
        return self.response


def test_osrm_table_parses_distance_and_duration_matrix(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "code": "Ok",
                "distances": [[0, 1200], [1200, 0]],
                "durations": [[0, 90], [95, 0]],
            }

    FakeAsyncClient.response = Response()
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client = OSRMClient()
    distances = asyncio.run(
        client.table(
            [
                Location(lat=35.1, lng=129.0),
                Location(lat=35.2, lng=129.1),
            ]
        )
    )
    assert distances == [[0.0, 1200.0], [1200.0, 0.0]]
    assert client.last_table_durations == [[0.0, 90.0], [95.0, 0.0]]


@pytest.mark.parametrize("code,error", [("NoRoute", NoRouteError), ("bad", InvalidRouteResponseError)])
def test_osrm_table_does_not_turn_route_failures_into_zero(monkeypatch, code, error):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"code": code}

    FakeAsyncClient.response = Response()
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    with pytest.raises(error):
        asyncio.run(
            OSRMClient().table(
                [Location(lat=35.1, lng=129.0), Location(lat=35.2, lng=129.1)]
            )
        )
