from backend.places import load_places, search_places


def result_names(query: str) -> list[str]:
    return [place.name for place in search_places(query)]


def test_city_index_comes_from_application_place_dataset() -> None:
    places = load_places()

    assert len(places) == 22
    busan = next(place for place in places if place.id == "city-busan")
    assert busan.lat == 35.1796
    assert busan.lng == 129.0756


def test_partial_korean_search() -> None:
    assert "부산" in result_names(" 부 ")
    assert "서울" in result_names("서울")


def test_case_insensitive_english_alias_search() -> None:
    assert result_names("seOuL") == ["서울"]
    assert result_names("Gangneung") == ["강릉"]


def test_empty_and_unknown_queries_return_no_results() -> None:
    assert search_places("   ") == []
    assert search_places("not-a-korean-city") == []


def test_search_results_are_limited() -> None:
    assert len(search_places("시", limit=3)) <= 3
