from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.accommodation.models import AccommodationOffer
from backend.entities.matcher import (
    compare_records,
    generate_candidate_pairs,
    source_record_id,
)
from backend.entities.models import EntitySourceRecord, MatchDecision
from backend.entities.normalize import normalize_address, normalize_name
from backend.entities.rating import (
    build_rating_profile,
    normalize_rating,
    source_confidence,
)
from backend.entities.resolver import EntityResolver
from backend.places.models import PlaceRecord as V07PlaceRecord

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def record(
    name: str,
    *,
    source: str = "visitkorea",
    source_id: str | None = None,
    category: str = "restaurant",
    address: str | None = "부산광역시 수영구 광안리로 1",
    lat: float | None = 35.153,
    lng: float | None = 129.118,
    rating: float | None = 4.5,
    rating_scale: float | None = 5.0,
    review_count: int | None = 100,
    fetched_at: datetime = NOW,
    **raw: object,
) -> EntitySourceRecord:
    return EntitySourceRecord(
        source=source,
        source_id=source_id,
        source_url=(
            f"https://www.booking.com/hotel/kr/{source_id}"
            if source == "booking" and source_id
            else f"https://english.visitkorea.or.kr/detail/{source_id}"
            if source == "visitkorea" and source_id
            else f"https://{source}.example/{source_id}"
            if source_id
            else None
        ),
        name=name,
        category=category,
        address=address,
        lat=lat,
        lng=lng,
        rating=rating,
        rating_scale=rating_scale,
        review_count=review_count,
        fetched_at=fetched_at,
        **raw,
    )


def test_multilingual_punctuation_normalization_and_bilingual_match() -> None:
    assert normalize_name("롯데호텔 부산") == "lotte hotel busan"
    assert normalize_name("LOTTE HOTEL BUSAN") == "lotte hotel busan"
    assert normalize_name("A-B") == "a b"
    assert normalize_name("A·B") == "a b"
    assert normalize_address("서울특별시 중구 1") == "seoul 중구 1"

    korean = record(
        "롯데호텔 부산",
        source="accommodation_source",
        source_id="kr-1",
        category="accommodation",
        address="부산광역시 중구 중앙대로 1",
        lat=35.10100,
        lng=129.03200,
        rating=None,
        rating_scale=None,
        review_count=None,
    )
    english = record(
        "Lotte Hotel Busan",
        source="other_source",
        source_id="en-1",
        category="accommodation",
        address="1 Jungang-daero, Jung-gu, Busan",
        lat=35.10110,
        lng=129.03205,
        rating=None,
        rating_scale=None,
        review_count=None,
    )
    decision = compare_records(korean, english)
    assert decision.decision is MatchDecision.MATCH
    assert decision.method == "NAME_ADDRESS_COORDINATE_MATCH"


def test_resolver_accepts_existing_v07_source_model_without_losing_raw_fields() -> None:
    existing = V07PlaceRecord(
        source="visitkorea",
        source_id="v07-1",
        source_url="https://english.visitkorea.or.kr/detail/v07-1",
        name="V0.7 Kitchen",
        category="restaurant",
        lat=35.153,
        lng=129.118,
        address="부산광역시 수영구 1",
        rating=4.7,
        rating_scale=5.0,
        review_count=123,
        fetched_at=NOW,
        subcategory="Food",
        raw_category="Food",
        opening_information="11:00-21:00",
        tags=["local"],
    )
    result = EntityResolver().resolve([existing], now=NOW)
    canonical = result.canonical_places[0]
    assert canonical.name == "V0.7 Kitchen"
    assert canonical.raw_fields["subcategory"] == ["Food"]
    assert result.source_records[canonical.sources[0].source_record_id].raw_category == "Food"


def test_similar_name_fixture_can_merge_only_with_location_evidence() -> None:
    same_tower = EntityResolver().resolve(
        [
            record("서울타워", source="a", source_id="tower-a", address="서울특별시 중구 남산 1", lat=37.5512, lng=126.9882),
            record("N서울타워", source="b", source_id="tower-b", address="서울특별시 중구 남산 1", lat=37.5512, lng=126.9882),
        ],
        now=NOW,
    )
    assert len(same_tower.canonical_places) == 1
    assert same_tower.canonical_places[0].source_count == 2


def test_name_only_and_location_only_are_not_automatic_merges() -> None:
    name_only = compare_records(
        record("Blue Harbor Cafe", source="a", source_id="1", address=None, lat=None, lng=None),
        record("Blue Harbor Cafe", source="b", source_id="2", address=None, lat=None, lng=None),
    )
    assert name_only.decision is MatchDecision.AMBIGUOUS

    location_only = compare_records(
        record("서울역", source="a", source_id="1", address="서울특별시 중구 1", lat=37.56, lng=126.98),
        record("서울역사박물관", source="b", source_id="2", address="서울특별시 중구 1", lat=37.56, lng=126.98),
    )
    assert location_only.decision is MatchDecision.NO_MATCH


def test_chain_branch_and_region_and_category_conflicts_are_hard_rejections() -> None:
    assert compare_records(
        record("스타벅스 강남점", source="a", source_id="1"),
        record("스타벅스 역삼점", source="b", source_id="2"),
    ).decision is MatchDecision.NO_MATCH
    assert compare_records(
        record("중앙시장", source="a", source_id="1", address="서울특별시 중구 1"),
        record("중앙시장", source="b", source_id="2", address="부산광역시 중구 1"),
    ).method == "REGION_CONFLICT"
    assert compare_records(
        record("롯데호텔 부산", source="a", source_id="1", category="accommodation"),
        record("롯데호텔 부산", source="b", source_id="2", category="restaurant"),
    ).method == "CATEGORY_CONFLICT"


def test_source_native_identity_is_prioritized_but_distinct_ids_are_not_collapsed() -> None:
    first = record("Changed Display Name", source="source", source_id="same")
    second = record("Completely Changed Display Name", source="source", source_id="same")
    assert compare_records(first, second).method == "EXACT_SOURCE_ID"
    assert compare_records(
        record("Same Restaurant", source="source", source_id="one"),
        record("Same Restaurant", source="source", source_id="two"),
    ).decision is MatchDecision.NO_MATCH


def test_refresh_of_one_source_id_keeps_newest_snapshot_without_duplicate_entity() -> None:
    old = record("Refresh Place", source="source", source_id="refresh", fetched_at=NOW - timedelta(days=1), rating=4.0)
    new = record("Refresh Place", source="source", source_id="refresh", fetched_at=NOW, rating=4.8)
    result = EntityResolver().resolve([old, new], now=NOW)
    assert result.stats.valid_records == 1
    assert result.stats.quarantined_records == 0
    assert result.source_records[next(iter(result.source_records))].rating == 4.8


def test_coordinate_threshold_and_null_coordinate_safety() -> None:
    close = compare_records(
        record("A Place", source="a", source_id="1", lat=35.0, lng=129.0),
        record("A Place", source="b", source_id="2", lat=35.0002, lng=129.0002),
    )
    assert close.decision is MatchDecision.MATCH
    far = compare_records(
        record("A Place", source="a", source_id="1", lat=35.0, lng=129.0),
        record("A Place", source="b", source_id="2", lat=35.03, lng=129.0),
    )
    assert far.method == "DISTANCE_CONFLICT"

    hotel_a = record("No Map Hotel", source="a", source_id="1", category="accommodation", lat=None, lng=None)
    hotel_b = record("No Map Hotel", source="b", source_id="2", category="accommodation", lat=None, lng=None)
    result = EntityResolver().resolve([hotel_a, hotel_b])
    assert len(result.canonical_places) == 1
    assert result.canonical_places[0].lat is None
    assert result.canonical_places[0].lng is None


def test_resolver_keeps_transitive_inconsistency_separate_with_override() -> None:
    a = record("Bridge Place", source="a", source_id="a", address=None, lat=None, lng=None)
    b = record("Bridge Place", source="b", source_id="b", address=None, lat=None, lng=None)
    c = record("Bridge Place", source="c", source_id="c", address=None, lat=None, lng=None)
    ids = [source_record_id(value) for value in (a, b, c)]
    overrides = {
        tuple(sorted((ids[0], ids[1]))): MatchDecision.MATCH,
        tuple(sorted((ids[1], ids[2]))): MatchDecision.MATCH,
        tuple(sorted((ids[0], ids[2]))): MatchDecision.NO_MATCH,
    }
    result = EntityResolver(overrides=overrides).resolve([c, a, b])
    assert len(result.canonical_places) == 2
    assert sorted(place.source_count for place in result.canonical_places) == [1, 2]


def test_grouping_is_deterministic_and_order_independent() -> None:
    values = [
        record("롯데호텔 부산", source="a", source_id="1", category="accommodation", rating=None, rating_scale=None, review_count=None),
        record("LOTTE HOTEL BUSAN", source="b", source_id="2", category="accommodation", rating=None, rating_scale=None, review_count=None),
        record("스타벅스 강남점", source="a", source_id="3", address="서울특별시 강남구 1", lat=37.5, lng=127.0),
        record("스타벅스 역삼점", source="b", source_id="4", address="서울특별시 강남구 2", lat=37.5, lng=127.001),
    ]
    first = EntityResolver().resolve(values, now=NOW)
    second = EntityResolver().resolve(list(reversed(values)), now=NOW)
    assert [place.id for place in first.canonical_places] == [place.id for place in second.canonical_places]
    assert [place.name for place in first.canonical_places] == [place.name for place in second.canonical_places]
    assert [place.source_count for place in first.canonical_places] == [place.source_count for place in second.canonical_places]


def test_raw_fields_membership_and_accommodation_offers_are_preserved_separately() -> None:
    place = record(
        "Sample Hotel",
        source="booking",
        source_id="hotel-1",
        category="accommodation",
        subcategory="hotel",
        opening_information="24h",
        tags=["sea view"],
        rating=None,
        rating_scale=None,
        review_count=None,
        lat=None,
        lng=None,
    )
    offer = AccommodationOffer(
        source="booking",
        source_offer_id="offer-1",
        place_source_id="hotel-1",
        checkin="2026-10-01",
        checkout="2026-10-02",
        adults=2,
        base_price_krw=100_000,
        taxes_krw=10_000,
        final_price_krw=110_000,
        availability=True,
        fetched_at=NOW,
    )
    result = EntityResolver().resolve([place], offers=[offer], now=NOW)
    canonical = result.canonical_places[0]
    assert canonical.raw_fields["subcategory"] == ["hotel"]
    assert canonical.raw_fields["opening_information"] == ["24h"]
    assert canonical.raw_fields["tags"] == [["sea view"]]
    assert canonical.sources[0].source_record_id in result.source_records
    assert len(result.offers_by_canonical_id[canonical.id]) == 1
    assert result.offers_by_canonical_id[canonical.id][0].final_price_krw == 110_000


def test_null_and_invalid_rating_semantics_and_scale_parity() -> None:
    assert normalize_rating(4.5, 5.0) == pytest.approx(0.9)
    assert normalize_rating(9.0, 10.0) == pytest.approx(0.9)
    assert normalize_rating(90.0, 100.0) == pytest.approx(0.9)
    assert normalize_rating(None, None) is None
    unknown_reviews = EntityResolver().resolve(
        [record("Known Rating", source="a", source_id="known", rating=4.5, rating_scale=5.0, review_count=None)],
        now=NOW,
    ).canonical_places[0]
    assert unknown_reviews.normalized_rating == pytest.approx(0.9)
    assert unknown_reviews.review_count_total is None
    with pytest.raises(ValueError):
        normalize_rating(11.0, 10.0)
    with pytest.raises(ValueError):
        normalize_rating(-1.0, 5.0)

    result = EntityResolver().resolve(
        [
            record("No Rating", source="a", source_id="1", rating=None, rating_scale=None, review_count=None),
            {
                "source": "b",
                "source_id": "2",
                "name": "Invalid Rating",
                "category": "restaurant",
                "address": "부산광역시 수영구 1",
                "lat": 35.1,
                "lng": 129.1,
                "rating": 11.0,
                "rating_scale": 10.0,
                "review_count": 1,
                "fetched_at": NOW,
            },
        ],
        now=NOW,
    )
    assert result.canonical_places[0].normalized_rating is None
    assert result.stats.quarantined_records == 1


def test_source_url_never_accepts_javascript_or_non_https_links() -> None:
    with pytest.raises(ValueError):
        EntitySourceRecord(
            source="x",
            source_id="1",
            source_url="javascript:alert(1)",
            name="Unsafe",
            category="restaurant",
            fetched_at=NOW,
        )
    with pytest.raises(ValueError):
        EntitySourceRecord(
            source="x",
            source_id="1",
            source_url="http://example.test/place",
            name="Unsafe",
            category="restaurant",
            fetched_at=NOW,
        )


def test_negative_or_absurd_review_counts_are_quarantined() -> None:
    base = {
        "source": "x",
        "name": "Review Count Test",
        "category": "restaurant",
        "fetched_at": NOW,
        "rating": 4.0,
        "rating_scale": 5.0,
    }
    result = EntityResolver().resolve(
        [
            {**base, "source_id": "negative", "review_count": -1},
            {**base, "source_id": "huge", "review_count": 1_000_000_001},
        ],
        now=NOW,
    )
    assert result.stats.valid_records == 0
    assert result.stats.quarantined_records == 2


def test_review_count_prior_and_confidence_prefer_large_observed_evidence() -> None:
    small = record("Review Place", source="a", source_id="1", rating=5.0, rating_scale=5.0, review_count=2)
    large = record("Review Place", source="b", source_id="2", rating=4.8, rating_scale=5.0, review_count=10_000)
    profile = build_rating_profile([small, large], source_record_ids={0: "a", 1: "b"})
    assert profile.prior == pytest.approx(0.98)
    assert profile.minimum_review_count == 10_000
    assert profile.normalized_rating is not None
    assert profile.normalized_rating < 0.98
    assert profile.observed_review_count_sum == 10_002
    assert profile.rating_confidence is not None and profile.rating_confidence > 0.5

    fresh = source_confidence(small, now=NOW)
    stale = source_confidence(small.model_copy(update={"fetched_at": NOW - timedelta(days=720)}), now=NOW)
    assert fresh > stale


def test_coordinate_conflict_is_flagged_and_not_averaged_into_a_marker() -> None:
    first = record("Conflict Place", source="a", source_id="1", lat=35.0, lng=129.0)
    second = record("Conflict Place", source="b", source_id="2", lat=35.01, lng=129.0)
    result = EntityResolver().resolve([first, second], now=NOW)
    assert len(result.canonical_places) == 1
    assert result.canonical_places[0].coordinate_conflict is True
    assert result.canonical_places[0].lat is None
    assert result.canonical_places[0].lng is None


def test_candidate_blocking_does_not_create_global_pair_matrix() -> None:
    records = [
        record(
            f"Place {index}",
            source="synthetic",
            source_id=str(index),
            address=f"서울특별시 중구 {index}",
            lat=37.50 + index * 0.001,
            lng=126.90,
        )
        for index in range(1_000)
    ]
    pairs = generate_candidate_pairs(records)
    assert len(pairs) < len(records) * 10
    assert len(pairs) < len(records) * (len(records) - 1) // 2


def test_fifty_pair_precision_first_fixture_audit() -> None:
    labeled: list[tuple[EntitySourceRecord, EntitySourceRecord, MatchDecision]] = []
    for index in range(10):
        labeled.append(
            (
                record(f"Venue {index}", source="a", source_id=f"match-a-{index}"),
                record(f"Venue {index}", source="b", source_id=f"match-b-{index}"),
                MatchDecision.MATCH,
            )
        )
    for index in range(10):
        labeled.append(
            (
                record(
                    "롯데호텔 부산",
                    source="a",
                    source_id=f"bilingual-a-{index}",
                    category="accommodation",
                    address="부산광역시 중구 중앙대로 1",
                    lat=35.101,
                    lng=129.032,
                    rating=None,
                    rating_scale=None,
                    review_count=None,
                ),
                record(
                    "LOTTE HOTEL BUSAN",
                    source="b",
                    source_id=f"bilingual-b-{index}",
                    category="accommodation",
                    address="1 Jungang-daero, Jung-gu, Busan",
                    lat=35.1011,
                    lng=129.0321,
                    rating=None,
                    rating_scale=None,
                    review_count=None,
                ),
                MatchDecision.MATCH,
            )
        )
    for index in range(5):
        labeled.append(
            (
                record("스타벅스 강남점", source="a", source_id=f"chain-a-{index}"),
                record("스타벅스 역삼점", source="b", source_id=f"chain-b-{index}"),
                MatchDecision.NO_MATCH,
            )
        )
    for index in range(5):
        labeled.append(
            (
                record(f"Cafe {index}", source="a", source_id=f"complex-a-{index}"),
                record(f"Museum {index}", source="b", source_id=f"complex-b-{index}"),
                MatchDecision.NO_MATCH,
            )
        )
    for index in range(5):
        labeled.append(
            (
                record("중앙시장", source="a", source_id=f"region-a-{index}", address="서울특별시 중구 1"),
                record("중앙시장", source="b", source_id=f"region-b-{index}", address="부산광역시 중구 1"),
                MatchDecision.NO_MATCH,
            )
        )
    for index in range(5):
        labeled.append(
            (
                record("Same Name", source="a", source_id=f"ambiguous-a-{index}", address=None, lat=None, lng=None),
                record("Same Name", source="b", source_id=f"ambiguous-b-{index}", address=None, lat=None, lng=None),
                MatchDecision.AMBIGUOUS,
            )
        )
    for index in range(5):
        labeled.append(
            (
                record("Same Facility", source="a", source_id=f"category-a-{index}", category="restaurant"),
                record("Same Facility", source="b", source_id=f"category-b-{index}", category="attraction"),
                MatchDecision.NO_MATCH,
            )
        )
    for index in range(5):
        labeled.append(
            (
                record("Far Venue", source="a", source_id=f"distance-a-{index}", lat=35.0, lng=129.0),
                record("Far Venue", source="b", source_id=f"distance-b-{index}", lat=35.04, lng=129.0),
                MatchDecision.NO_MATCH,
            )
        )
    outcomes = [compare_records(first, second).decision for first, second, _expected in labeled]
    expected = [expected for _first, _second, expected in labeled]
    assert len(labeled) == 50
    assert outcomes == expected
    assert sum(outcome is MatchDecision.MATCH for outcome in outcomes) == 20
    assert sum(outcome is MatchDecision.NO_MATCH for outcome in outcomes) == 25
    assert sum(outcome is MatchDecision.AMBIGUOUS for outcome in outcomes) == 5
