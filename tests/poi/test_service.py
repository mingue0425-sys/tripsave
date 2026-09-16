from datetime import datetime, timezone

from backend.models import Location
from backend.poi.models import PoiCategory, PoiRecord, PoiSearchRequest
from backend.poi.repository import PoiRepository
from backend.poi.service import PoiService
from backend.routing.models import RouteGeometry, RouteResult


def _record(record_id: str, category: PoiCategory, lat: float, lng: float) -> PoiRecord:
    return PoiRecord(
        id=record_id,
        source="fixture",
        source_id=record_id,
        name=record_id,
        category=category,
        lat=lat,
        lng=lng,
        address="부산 테스트 주소",
        metadata={"raw": "fixture"},
        fetched_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )


def _request(*, route: RouteResult | None = None, categories=None) -> PoiSearchRequest:
    return PoiSearchRequest(
        destination=Location(lat=36.0, lng=127.0, label="fixture"),
        route=route,
        categories=categories or [PoiCategory.HOSPITAL, PoiCategory.PHARMACY],
        destination_radius_m=1_000,
        route_corridor_m=150,
        limit_per_category=50,
    )


def test_destination_search_distinguishes_success_empty(tmp_path):
    repository = PoiRepository(tmp_path / "poi.sqlite3")
    repository.replace_records(
        [
            _record("near-hospital", PoiCategory.HOSPITAL, 36.0, 127.0),
            _record("far-hospital", PoiCategory.HOSPITAL, 36.1, 127.0),
            _record("near-pharmacy", PoiCategory.PHARMACY, 36.004, 127.0),
        ]
    )

    response = __import__("asyncio").run(PoiService(repository).search(_request()))

    assert response.complete is True
    assert {record.id for record in response.results} == {"near-hospital", "near-pharmacy"}
    assert response.category_statuses == {"hospital": "ok", "pharmacy": "ok"}
    assert all(record.distance_to_destination_m is not None for record in response.results)

    empty = __import__("asyncio").run(
        PoiService(repository).search(
            _request(categories=[PoiCategory.PARKING])
        )
    )
    assert empty.complete is True
    assert empty.results == []
    assert empty.category_statuses == {"parking": "empty"}


def test_route_corridor_includes_route_poi_outside_destination_radius(tmp_path):
    repository = PoiRepository(tmp_path / "poi.sqlite3")
    repository.replace_records(
        [
            _record("corridor", PoiCategory.HOSPITAL, 36.0, 126.95),
            _record("outside", PoiCategory.HOSPITAL, 36.02, 126.95),
        ]
    )
    route = RouteResult(
        distance_m=10_000,
        duration_s=900,
        geometry=RouteGeometry(
            type="LineString", coordinates=[[126.9, 36.0], [127.0, 36.0]]
        ),
    )

    response = __import__("asyncio").run(
        PoiService(repository).search(_request(route=route, categories=[PoiCategory.HOSPITAL]))
    )

    assert [record.id for record in response.results] == ["corridor"]
    assert response.results[0].distance_to_route_m is not None


def test_missing_index_is_unavailable_not_empty(tmp_path):
    service = PoiService(PoiRepository(tmp_path / "missing.sqlite3"))

    response = __import__("asyncio").run(
        service.search(_request(categories=[PoiCategory.HOSPITAL]))
    )

    assert response.complete is False
    assert response.results == []
    assert response.category_statuses == {"hospital": "unavailable"}
    assert response.warnings == ["POI_INDEX_UNAVAILABLE"]


def test_poi_namespace_accepts_namespaced_input():
    request = PoiSearchRequest(
        destination={"lat": 36.0, "lng": 127.0},
        categories=["poi:hospital"],
    )
    assert request.categories == [PoiCategory.HOSPITAL]
