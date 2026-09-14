import asyncio

import httpx
import pytest

from crawler.places.base import PlaceDestination
from crawler.places.errors import SourceHTTPError, SourcePageChangedError, SourceTimeoutError
from crawler.places.visitkorea import SEARCH_URL, VisitKoreaSource
from tests.places.fixtures import ATTRACTION_DETAIL_HTML, DETAIL_HTML, EMPTY_SEARCH_HTML, SEARCH_HTML


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class FakeClient:
    def __init__(self, *, detail_html: str = DETAIL_HTML, search_html: str = SEARCH_HTML):
        self.detail_html = detail_html
        self.search_html = search_html
        self.urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, params=None):
        self.urls.append(str(url))
        if str(url) == SEARCH_URL:
            return FakeResponse(self.search_html)
        return FakeResponse(self.detail_html)


def run(coroutine):
    return asyncio.run(coroutine)


def test_visitkorea_source_searches_public_page_then_detail_pages() -> None:
    fake_client = FakeClient()
    source = VisitKoreaSource(
        max_results=1,
        request_interval_seconds=0,
        client_factory=lambda: fake_client,
    )
    records = run(
        source.search(
            PlaceDestination(37.5665, 126.978, "Seoul"),
            "restaurant",
            10,
        )
    )

    assert len(records) == 1
    assert records[0].source == "visitkorea"
    assert records[0].name == "Fixture Kitchen"
    assert len(fake_client.urls) == 2
    assert fake_client.urls[0] == SEARCH_URL
    assert source.last_issues == []


def test_visitkorea_source_returns_normal_empty_results() -> None:
    fake_client = FakeClient(search_html=EMPTY_SEARCH_HTML)
    source = VisitKoreaSource(
        max_results=1,
        request_interval_seconds=0,
        client_factory=lambda: fake_client,
    )
    records = run(
        source.search(
            PlaceDestination(37.5665, 126.978, "Seoul"),
            "restaurant",
            10,
        )
    )
    assert records == []
    assert source.last_issues == []


def test_visitkorea_source_keeps_partial_results_when_one_detail_changes() -> None:
    class PartialClient(FakeClient):
        async def get(self, url, params=None):
            self.urls.append(str(url))
            if str(url) == SEARCH_URL:
                return FakeResponse(SEARCH_HTML)
            if "12345" in str(url):
                return FakeResponse(DETAIL_HTML)
            return FakeResponse("<html><body><h1></h1></body></html>")

    fake_client = PartialClient()
    source = VisitKoreaSource(
        max_results=2,
        request_interval_seconds=0,
        client_factory=lambda: fake_client,
    )
    records = run(
        source.search(
            PlaceDestination(37.5665, 126.978, "Seoul"),
            "restaurant",
            10,
        )
    )
    assert len(records) == 1
    assert source.last_issues[0].code == "PARSER_ERROR"


def test_timeout_and_http_errors_are_typed() -> None:
    class TimeoutClient(FakeClient):
        async def get(self, url, params=None):
            raise httpx.ReadTimeout("timed out")

    timeout_source = VisitKoreaSource(
        client_factory=lambda: TimeoutClient(), request_interval_seconds=0
    )
    with pytest.raises(SourceTimeoutError):
        run(timeout_source.search(PlaceDestination(37.5, 127.0, "Seoul"), "restaurant", 10))

    class ErrorClient(FakeClient):
        async def get(self, url, params=None):
            return FakeResponse("blocked", status_code=403)

    error_source = VisitKoreaSource(
        client_factory=lambda: ErrorClient(), request_interval_seconds=0
    )
    with pytest.raises(SourceHTTPError):
        run(error_source.search(PlaceDestination(37.5, 127.0, "Seoul"), "restaurant", 10))


def test_page_changed_error_is_not_reported_as_empty() -> None:
    source = VisitKoreaSource(
        client_factory=lambda: FakeClient(search_html="<html><body>changed</body></html>"),
        request_interval_seconds=0,
    )
    with pytest.raises(SourcePageChangedError):
        run(source.search(PlaceDestination(37.5, 127.0, "Seoul"), "attraction", 30))
