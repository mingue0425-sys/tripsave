from __future__ import annotations

from fastapi.testclient import TestClient

import app as app_module
from backend.entities.repository import EntityResolutionRepository


def test_entity_resolution_api_persists_and_canonical_read_api(monkeypatch, tmp_path) -> None:
    repository = EntityResolutionRepository(tmp_path / "api.sqlite3")
    monkeypatch.setattr(app_module, "entity_repository", repository)
    monkeypatch.setattr(app_module, "TOLL_DEBUG_MODE", True)
    client = TestClient(app_module.app)
    payload = {
        "records": [
            {
                "source": "a",
                "source_id": "1",
                "source_url": "https://a.example/1",
                "name": "롯데호텔 부산",
                "category": "accommodation",
                "lat": 35.101,
                "lng": 129.032,
                "address": "부산광역시 중구 중앙대로 1",
                "rating": None,
                "rating_scale": None,
                "review_count": None,
                "fetched_at": "2026-09-15T12:00:00Z",
                "subcategory": "hotel",
            },
            {
                "source": "b",
                "source_id": "2",
                "source_url": "https://b.example/2",
                "name": "LOTTE HOTEL BUSAN",
                "category": "accommodation",
                "lat": 35.1011,
                "lng": 129.0321,
                "address": "1 Jungang-daero, Jung-gu, Busan",
                "rating": None,
                "rating_scale": None,
                "review_count": None,
                "fetched_at": "2026-09-15T12:00:00Z",
            },
        ]
    }
    response = client.post("/api/entities/resolve", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["stats"]["canonical_count"] == 1
    assert body["canonical_places"][0]["source_count"] == 2
    assert body["source_records"]

    read_response = client.get("/api/places/canonical", params={"category": "accommodation"})
    assert read_response.status_code == 200
    assert read_response.json()["count"] == 1

    debug_response = client.get(f"/api/entities/debug/{body['canonical_places'][0]['id']}")
    assert debug_response.status_code == 200
    assert len(debug_response.json()["members"]) == 2
