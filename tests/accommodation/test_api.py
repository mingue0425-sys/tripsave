import app as app_module
from fastapi.testclient import TestClient

from backend.accommodation.models import AccommodationSearchResponse
from backend.accommodation.service import ACCOMMODATION_API_URL


client = TestClient(app_module.app)


class StubAccommodationService:
    async def search(self, request):
        return AccommodationSearchResponse(
            complete=True,
            results=[],
            source_status="fresh",
            cache_hit=False,
            fetched_at="2026-09-12T00:00:00Z",
            issues=[],
        )


def test_accommodation_api_accepts_contract_and_returns_envelope(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "accommodation_service", StubAccommodationService())

    response = client.post(
        ACCOMMODATION_API_URL,
        json={
            "destination": {"lat": 35.1796, "lng": 129.0756, "label": "Busan"},
            "checkin": "2026-10-01",
            "checkout": "2026-10-02",
            "adults": 2,
        },
    )

    assert response.status_code == 200
    assert response.json()["complete"] is True
    assert response.json()["results"] == []


def test_accommodation_api_invalid_dates_are_422() -> None:
    response = client.post(
        ACCOMMODATION_API_URL,
        json={
            "destination": {"lat": 35.1796, "lng": 129.0756, "label": "Busan"},
            "checkin": "2026-10-02",
            "checkout": "2026-10-01",
            "adults": 2,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
