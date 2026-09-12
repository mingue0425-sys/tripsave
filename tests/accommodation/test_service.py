import asyncio

import pytest

from backend.accommodation.models import AccommodationSearchRequest
from backend.accommodation.service import AccommodationService
from backend.models import Location
from crawler.accommodation.errors import AccommodationPageChangedError

from .helpers import make_result


def make_request() -> AccommodationSearchRequest:
    return AccommodationSearchRequest(
        destination=Location(lat=37.5665, lng=126.978, label="Seoul"),
        checkin="2026-10-01",
        checkout="2026-10-02",
        adults=2,
    )


class FakeSource:
    source_name = "fake"

    def __init__(self, results=None, error=None):
        self.results = results
        self.error = error
        self.calls = 0

    async def search(self, destination, checkin, checkout, adults, children=0):
        self.calls += 1
        if self.error:
            raise self.error
        return [] if self.results is None else self.results

    async def close(self):
        return None


class SlowSource(FakeSource):
    async def search(self, destination, checkin, checkout, adults, children=0):
        self.calls += 1
        await asyncio.sleep(0.1)
        return []


def test_service_uses_offer_cache_on_second_search(tmp_path) -> None:
    source = FakeSource(results=[make_result(source="fake")])
    service = AccommodationService(
        database_path=tmp_path / "accommodation.db",
        sources=[source],
    )

    first, second = asyncio.run(_search_twice(service))

    assert first.complete is True
    assert first.source_status == "fresh"
    assert second.complete is True
    assert second.source_status == "cache"
    assert second.cache_hit is True
    assert source.calls == 1


def test_service_preserves_partial_results_when_price_is_unknown(tmp_path) -> None:
    source = FakeSource(results=[make_result(source="fake", final_price_krw=None, availability=None)])
    service = AccommodationService(database_path=tmp_path / "accommodation.db", sources=[source])

    response = asyncio.run(service.search(make_request()))

    assert response.complete is False
    assert response.source_status == "partial"
    assert response.results[0].offers[0].final_price_krw is None


def test_service_maps_page_change_to_explicit_incomplete_issue(tmp_path) -> None:
    source = FakeSource(error=AccommodationPageChangedError("internal details"))
    service = AccommodationService(database_path=tmp_path / "accommodation.db", sources=[source])

    response = asyncio.run(service.search(make_request()))

    assert response.complete is False
    assert response.source_status == "unavailable"
    assert response.results == []
    assert response.issues[0].code == "ACCOMMODATION_PAGE_CHANGED"
    assert "내부" not in response.issues[0].message


def test_service_maps_timeout_without_returning_zero_price(tmp_path) -> None:
    source = SlowSource()
    service = AccommodationService(
        database_path=tmp_path / "accommodation.db",
        sources=[source],
        source_timeout_s=1,
    )
    service.source_timeout_s = 0.01

    response = asyncio.run(service.search(make_request()))

    assert response.complete is False
    assert response.issues[0].code == "ACCOMMODATION_SOURCE_TIMEOUT"
    assert all(
        offer.final_price_krw != 0
        for result in response.results
        for offer in result.offers
    )


def test_empty_source_result_is_complete_and_not_an_error(tmp_path) -> None:
    source = FakeSource(results=[])
    service = AccommodationService(database_path=tmp_path / "accommodation.db", sources=[source])

    response = asyncio.run(service.search(make_request()))

    assert response.complete is True
    assert response.source_status == "fresh"
    assert response.results == []


def test_service_applies_requested_radius_only_to_source_reported_distance(tmp_path) -> None:
    source = FakeSource(results=[make_result(source="fake")])
    service = AccommodationService(database_path=tmp_path / "accommodation.db", sources=[source])
    request = make_request().model_copy(update={"radius_km": 1.0})

    response = asyncio.run(service.search(request))

    assert response.complete is True
    assert response.results == []


async def _search_twice(service: AccommodationService):
    return await service.search(make_request()), await service.search(make_request())
