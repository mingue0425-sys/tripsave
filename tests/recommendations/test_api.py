from fastapi.testclient import TestClient

import app as app_module
from backend.trips.service import TripCandidateService
from tests.trips.helpers import make_offer, make_place, make_request


def test_recommendations_api_ranks_a_persisted_candidate_set_without_external_services() -> None:
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
    request = make_request(
        canonical_places=[hotel_a, hotel_b],
        offers=[
            make_offer(source_id="hotel-a", source_offer_id="offer-a", final_price_krw=200_000),
            make_offer(source_id="hotel-b", source_offer_id="offer-b", final_price_krw=300_000),
        ],
    )

    with TestClient(app_module.app) as client:
        assembly = client.post(
            "/api/trips/candidates",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        assert assembly.status_code == 200
        assembled = assembly.json()
        candidates = assembled["candidates"]
        response = client.post(
            "/api/recommendations/rank",
            json={
                "candidate_set_id": assembled["candidate_set_id"],
                "candidate_ids": [candidate["id"] for candidate in candidates],
                "request_fingerprint": assembled["request_fingerprint"],
                "mode": "lowest_cost",
                "limit": 2,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "lowest_cost"
    assert body["ranking_version"] == "v1.0.0"
    assert body["candidate_set_id"] == assembled["candidate_set_id"]
    assert body["request_fingerprint"] == assembled["request_fingerprint"]
    assert body["recommendations"][0]["candidate_id"] == candidates[0]["id"]
    assert body["recommendations"][0]["explanation"]
    assert body["weights"]["cost"] == 1.0


def test_recommendations_api_accepts_candidate_ids_from_their_own_set() -> None:
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
        assembled = assembly.json()
        candidate_id = assembled["candidates"][0]["id"]
        ranked = client.post(
            "/api/recommendations/rank",
            json={
                "candidate_set_id": assembled["candidate_set_id"],
                "candidate_ids": [candidate_id],
                "request_fingerprint": assembled["request_fingerprint"],
                "mode": "balanced",
            },
        )

    assert ranked.status_code == 200
    assert ranked.json()["recommendations"][0]["candidate_id"] == candidate_id


def test_recommendations_api_rejects_a_fingerprint_for_another_request() -> None:
    service = TripCandidateService()
    saved = app_module.candidate_set_store.save(service.assemble(make_request()))

    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/recommendations/rank",
            json={
                "candidate_set_id": saved.id,
                "candidate_ids": saved.candidate_ids,
                "request_fingerprint": "0" * 64,
                "mode": "balanced",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANDIDATE_SET_FINGERPRINT_MISMATCH"


def test_recommendations_api_rejects_full_client_candidates_or_invalid_requests() -> None:
    with TestClient(app_module.app) as client:
        empty = client.post("/api/recommendations/rank", json={"mode": "balanced"})
        spoofed = client.post(
            "/api/recommendations/rank",
            json={
                "candidate_set_id": "cs_not-a-real-set-1234567890",
                "candidates": [],
                "mode": "balanced",
            },
        )
        invalid = client.post(
            "/api/recommendations/rank",
            json={
                "candidate_set_id": "cs_not-a-real-set-1234567890",
                "custom_weights": {"unknown": 1},
            },
        )

    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "INVALID_REQUEST"
    assert spoofed.status_code == 422
    assert spoofed.json()["error"]["code"] == "INVALID_REQUEST"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"


def test_multiple_candidate_sets_are_isolated_and_both_remain_rankable() -> None:
    service = TripCandidateService()
    first = service.assemble(make_request())
    second = service.assemble(
        make_request(start_date="2026-11-01", end_date="2026-11-03")
    )
    first_set = app_module.candidate_set_store.save(first)
    second_set = app_module.candidate_set_store.save(second)

    with TestClient(app_module.app) as client:
        ranked_first = client.post(
            "/api/recommendations/rank",
            json={
                "candidate_set_id": first_set.id,
                "candidate_ids": first_set.candidate_ids,
                "mode": "balanced",
            },
        )
        ranked_second = client.post(
            "/api/recommendations/rank",
            json={
                "candidate_set_id": second_set.id,
                "candidate_ids": second_set.candidate_ids,
                "mode": "balanced",
            },
        )

    assert ranked_first.status_code == 200
    assert ranked_second.status_code == 200
    assert ranked_first.json()["candidate_set_id"] == first_set.id
    assert ranked_second.json()["candidate_set_id"] == second_set.id


def test_cross_set_candidate_id_is_rejected() -> None:
    service = TripCandidateService()
    first_set = app_module.candidate_set_store.save(service.assemble(make_request()))
    second_set = app_module.candidate_set_store.save(
        service.assemble(make_request(start_date="2026-12-01", end_date="2026-12-03"))
    )

    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/recommendations/rank",
            json={
                "candidate_set_id": first_set.id,
                "candidate_ids": second_set.candidate_ids,
                "mode": "balanced",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANDIDATE_SET_MISMATCH"
