from fastapi.testclient import TestClient

import app as app_module
from backend.trips.service import TripCandidateService
from tests.trips.helpers import make_offer, make_place, make_request


def test_recommendations_api_ranks_supplied_candidates_without_external_services() -> None:
    hotel_a = make_place(
        "hotel-a",
        "Hotel A",
        "accommodation",
        source="booking",
        source_id="hotel-a",
    )
    hotel_b = make_place(
        "hotel-b",
        "Hotel B",
        "accommodation",
        source="booking",
        source_id="hotel-b",
        rating=0.95,
    )
    candidates = TripCandidateService().assemble(
        make_request(
            canonical_places=[hotel_a, hotel_b],
            offers=[
                make_offer(source_id="hotel-a", source_offer_id="offer-a", final_price_krw=200_000),
                make_offer(source_id="hotel-b", source_offer_id="offer-b", final_price_krw=300_000),
            ],
        )
    ).candidates

    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/recommendations/rank",
            json={
                "mode": "lowest_cost",
                "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
                "limit": 2,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "lowest_cost"
    assert body["ranking_version"] == "v1.0.0"
    assert body["recommendations"][0]["candidate_id"] == candidates[0].id
    assert body["recommendations"][0]["explanation"]
    assert body["weights"]["cost"] == 1.0


def test_recommendations_api_accepts_candidate_ids_from_local_registry() -> None:
    hotel = make_place(
        "registry-hotel",
        "Registry Hotel",
        "accommodation",
        source="booking",
        source_id="registry-hotel",
    )
    request = make_request(
        canonical_places=[hotel],
        offers=[make_offer(source_id="registry-hotel", source_offer_id="registry-offer")],
    )

    with TestClient(app_module.app) as client:
        assembly = client.post(
            "/api/trips/candidates",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        assert assembly.status_code == 200
        candidate_id = assembly.json()["candidates"][0]["id"]
        ranked = client.post(
            "/api/recommendations/rank",
            json={"mode": "balanced", "candidate_ids": [candidate_id]},
        )

    assert ranked.status_code == 200
    assert ranked.json()["recommendations"][0]["candidate_id"] == candidate_id


def test_recommendations_api_rejects_empty_or_invalid_weights() -> None:
    with TestClient(app_module.app) as client:
        empty = client.post("/api/recommendations/rank", json={"mode": "balanced"})
        invalid = client.post(
            "/api/recommendations/rank",
            json={
                "mode": "balanced",
                "candidates": [],
                "custom_weights": {"unknown": 1},
            },
        )

    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "INVALID_REQUEST"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"


def test_new_candidate_assembly_invalidates_old_candidate_ids() -> None:
    first = TripCandidateService().assemble(make_request())
    second = TripCandidateService().assemble(make_request(start_date="2026-11-01", end_date="2026-11-03"))
    app_module._remember_candidates(first)
    app_module._remember_candidates(second)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/recommendations/rank",
            json={"mode": "balanced", "candidate_ids": [first.candidates[0].id]},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CANDIDATES_NOT_FOUND"
