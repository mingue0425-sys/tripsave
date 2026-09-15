from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from backend.entities.repository import EntityResolutionRepository
from backend.entities.resolver import EntityResolver
from tests.entities.test_resolution import NOW, record


def test_repository_persists_canonical_members_candidates_raw_and_offers(tmp_path) -> None:
    place_a = record(
        "롯데호텔 부산",
        source="a",
        source_id="hotel-a",
        category="accommodation",
        address="부산광역시 중구 중앙대로 1",
        lat=35.101,
        lng=129.032,
        rating=None,
        rating_scale=None,
        review_count=None,
        subcategory="hotel",
    )
    place_b = record(
        "LOTTE HOTEL BUSAN",
        source="b",
        source_id="hotel-b",
        category="accommodation",
        address="1 Jungang-daero, Jung-gu, Busan",
        lat=35.1011,
        lng=129.0321,
        rating=None,
        rating_scale=None,
        review_count=None,
        subcategory="hotel",
    )
    result = EntityResolver().resolve([place_b, place_a], now=NOW)
    repository = EntityResolutionRepository(tmp_path / "entities.sqlite3")
    repository.persist(result)
    stored = repository.list_canonical()
    assert len(stored) == 1
    assert stored[0].id == result.canonical_places[0].id
    assert len(stored[0].sources) == 2
    assert stored[0].raw_fields["subcategory"] == ["hotel"]
    debug = repository.debug(stored[0].id)
    assert debug is not None
    assert len(debug["members"]) == 2
    assert {member["name"] for member in debug["members"]} == {"롯데호텔 부산", "LOTTE HOTEL BUSAN"}
    repository.close()


def test_rebuild_is_order_independent_and_canonical_id_is_stable(tmp_path) -> None:
    first = record("Stable Place", source="a", source_id="stable-1")
    second = record("Stable Place", source="b", source_id="stable-2")
    first_result = EntityResolver().resolve([first, second], now=NOW)
    second_result = EntityResolver().resolve([second, first], now=NOW)
    assert first_result.canonical_places[0].id == second_result.canonical_places[0].id

    repository = EntityResolutionRepository(tmp_path / "rebuild.sqlite3")
    repository.persist(first_result)
    repository.persist(second_result)
    assert [place.id for place in repository.list_canonical()] == [first_result.canonical_places[0].id]


def test_two_resolvers_can_persist_without_sqlite_lock_errors(tmp_path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    result = EntityResolver().resolve([record("Concurrent Place", source="a", source_id="1")], now=NOW)

    def write_once() -> None:
        EntityResolutionRepository(path).persist(result)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _index: write_once(), range(2)))
    assert len(EntityResolutionRepository(path).list_canonical()) == 1
