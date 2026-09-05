from backend.tolls.names import normalize_toll_name, official_query_name


def test_normalization_preserves_distinct_stem_names() -> None:
    assert normalize_toll_name("서울TG") == normalize_toll_name("서울 요금소")
    assert normalize_toll_name("서울") != normalize_toll_name("서울산")


def test_official_query_removes_only_terminal_gate_suffix() -> None:
    assert official_query_name("서울 요금소") == "서울"
    assert official_query_name("Seoul TG") == "Seoul"
    assert official_query_name("서울산 요금소 북측") == "서울산 요금소 북측"
