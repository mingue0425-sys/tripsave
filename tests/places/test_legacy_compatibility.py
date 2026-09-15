from backend.city_search import load_places as city_load_places
from backend.city_search import search_places as city_search_places
from backend.places import load_places, search_places


def test_package_namespace_keeps_v05_local_city_search() -> None:
    assert len(load_places()) == 22
    assert search_places("Busan")[0].id == "city-busan"


def test_explicit_city_search_module_is_the_legacy_implementation() -> None:
    assert city_load_places()[0].id == load_places()[0].id
    assert city_search_places("Busan")[0].id == search_places("Busan")[0].id
