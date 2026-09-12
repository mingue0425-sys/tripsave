from backend.places import load_places, search_places


def test_package_namespace_keeps_v05_local_city_search() -> None:
    assert len(load_places()) == 22
    assert search_places("Busan")[0].id == "city-busan"
