import asyncio
from datetime import datetime, timedelta, timezone

from backend.places.models import PlaceSearchRequest
from backend.places.service import PlaceService
from crawler.places.base import SourceIssue
from crawler.places.cache import PlaceCache, make_cache_key
from crawler.places.errors import SourceTimeoutError
from tests.places.fixtures import FETCHED_AT, record_payload
from backend.places.models import PlaceRecord


def run(coroutine):
    return asyncio.run(coroutine)


def request(*categories: str) -> PlaceSearchRequest:
    return PlaceSearchRequest(
        destination={"lat": 35.1796, "lng": 129.0756, "label": "Busan"},
        categories=list(categories),
    )


class FakeSource:
    name = "fake"

    def __init__(self, results=None, error=None, issues=None):
        self.results = results or []
        self.error = error
        self.last_issues = issues or []
        self.calls = 0

    async def search(self, destination, category, radius_km):
        self.calls += 1
        if self.error:
            raise self.error
        return self.results


def test_service_distinguishes_successful_empty_results_from_failure(tmp_path) -> None:
    empty_source = FakeSource()
    service = PlaceService(sources=[empty_source], cache=PlaceCache(tmp_path / "empty.sqlite3"))
    empty_response = run(service.search(request("restaurant")))
    assert empty_response.complete is True
    assert empty_response.results == []
    assert empty_response.issues == []
    assert empty_response.source_status == "empty"

    failing_source = FakeSource(error=SourceTimeoutError())
    failing_service = PlaceService(
        sources=[failing_source], cache=PlaceCache(tmp_path / "failure.sqlite3")
    )
    failure_response = run(failing_service.search(request("restaurant")))
    assert failure_response.complete is False
    assert failure_response.results == []
    assert failure_response.issues[0].code == "SOURCE_TIMEOUT"
    assert failure_response.source_status == "unavailable"


def test_service_reports_partial_results_and_caches_only_complete_fetches(tmp_path) -> None:
    record = PlaceRecord.model_validate(record_payload())
    source = FakeSource(
        results=[record],
        issues=[SourceIssue(code="PARSER_ERROR", message="one detail changed")],
    )
    cache = PlaceCache(tmp_path / "partial.sqlite3")
    service = PlaceService(sources=[source], cache=cache)
    response = run(service.search(request("restaurant")))
    assert response.complete is False
    assert response.results[0].name == "Fixture Kitchen"
    assert response.source_status == "partial"
    assert source.calls == 1

    # The partial fetch is retried instead of being silently served as fresh.
    response_again = run(service.search(request("restaurant")))
    assert response_again.complete is False
    assert source.calls == 2


def test_service_uses_stale_cache_as_explicit_fallback(tmp_path) -> None:
    record = PlaceRecord.model_validate(record_payload())
    cache = PlaceCache(tmp_path / "stale.sqlite3")
    key = make_cache_key(
        source="fake",
        category="restaurant",
        label="Busan",
        lat=35.1796,
        lng=129.0756,
        radius_km=10,
    )
    cache.put(
        key,
        source="fake",
        category="restaurant",
        results=[record],
        fetched_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    source = FakeSource(error=SourceTimeoutError())
    service = PlaceService(sources=[source], cache=cache)
    response = run(service.search(request("restaurant")))
    assert response.complete is False
    assert response.cache_hit is True
    assert response.results[0].name == "Fixture Kitchen"
    assert response.issues[0].code == "STALE_CACHE_FALLBACK"
