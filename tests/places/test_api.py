import app as app_module
from fastapi.testclient import TestClient

from backend.places.models import PlaceSearchResponse


client = TestClient(app_module.app)


class StubPlaceService:
    async def search(self, request):
        return PlaceSearchResponse(
            complete=True,
            results=[],
            issues=[],
            cache_hit=False,
            source_status="empty",
            fetched_at="2026-09-12T00:00:00Z",
        )


def test_places_post_api_accepts_contract(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "places_service", StubPlaceService())
    response = client.post(
        "/api/places/search",
        json={
            "destination": {"lat": 35.1796, "lng": 129.0756, "label": "부산"},
            "categories": ["restaurant", "attraction"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "complete": True,
        "results": [],
        "issues": [],
        "cache_hit": False,
        "source_status": "empty",
        "fetched_at": "2026-09-12T00:00:00Z",
    }


def test_places_post_api_invalid_category_is_422() -> None:
    response = client.post(
        "/api/places/search",
        json={
            "destination": {"lat": 35.1796, "lng": 129.0756, "label": "Busan"},
            "categories": ["hotel"],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
