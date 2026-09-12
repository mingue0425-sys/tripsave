from backend.places.models import PlaceCategory
from crawler.places.errors import SourcePageChangedError
from crawler.places.parser import (
    parse_coordinates,
    parse_place_detail,
    parse_rating,
    parse_review_count,
    parse_search_cards,
)
from tests.places.fixtures import (
    ATTRACTION_DETAIL_HTML,
    DETAIL_HTML,
    FETCHED_AT,
    SEARCH_HTML,
)


def test_rating_parser_keeps_explicit_source_scale() -> None:
    assert parse_rating("4.7 of 5 bubbles") == (4.7, 5.0)
    assert parse_rating("9.2 / 10") == (9.2, 10.0)
    assert parse_rating("4.7 / 5") == (4.7, 5.0)


def test_review_count_parser_handles_thousands_and_compact_values() -> None:
    assert parse_review_count("(2,073 reviews)") == 2073
    assert parse_review_count("Based on 9.2K travelers") == 9200
    assert parse_review_count("no review data") is None


def test_coordinate_parser_uses_source_values() -> None:
    assert parse_coordinates("var lat = 35.1796; var lot = 129.0756;") == (
        35.1796,
        129.0756,
    )


def test_search_parser_reads_both_semantic_category_lists() -> None:
    restaurants = parse_search_cards(
        SEARCH_HTML,
        PlaceCategory.RESTAURANT,
        base_url="https://english.visitkorea.or.kr",
    )
    attractions = parse_search_cards(
        SEARCH_HTML,
        PlaceCategory.ATTRACTION,
        base_url="https://english.visitkorea.or.kr",
    )

    assert restaurants[0].source_id == "12345"
    assert restaurants[0].name == "Fixture Kitchen"
    assert restaurants[0].raw_category == "Food"
    assert attractions[0].source_id == "67890"
    assert attractions[0].category == PlaceCategory.ATTRACTION


def test_detail_parser_reads_json_ld_metadata_and_rating() -> None:
    record = parse_place_detail(
        DETAIL_HTML,
        PlaceCategory.RESTAURANT,
        source="visitkorea",
        source_id="12345",
        source_url="https://english.visitkorea.or.kr/detail/12345",
        fetched_at=FETCHED_AT,
        raw_category="Food",
    )

    assert record.name == "Fixture Kitchen"
    assert record.category == PlaceCategory.RESTAURANT
    assert record.address == "1 Sample-ro, Jung-gu, Seoul"
    assert (record.lat, record.lng) == (37.5665, 126.978)
    assert (record.rating, record.rating_scale, record.review_count) == (4.7, 5.0, 1234)
    assert record.opening_information == "11:00-21:00"
    assert record.subcategory == "Food"  # source-native value is not normalized


def test_detail_parser_keeps_missing_rating_as_null() -> None:
    html = DETAIL_HTML.replace('"aggregateRating": {', '"removedAggregateRating": {')
    record = parse_place_detail(
        html,
        PlaceCategory.ATTRACTION,
        source="visitkorea",
        source_id="67890",
        source_url="https://english.visitkorea.or.kr/detail/67890",
        fetched_at=FETCHED_AT,
    )
    assert record.rating is None
    assert record.rating_scale is None
    assert record.review_count is None


def test_page_changed_search_markup_is_explicit() -> None:
    try:
        parse_search_cards(
            "<html><body><div class='changed'></div></body></html>",
            PlaceCategory.RESTAURANT,
            base_url="https://english.visitkorea.or.kr",
        )
    except SourcePageChangedError as error:
        assert "category container" in str(error)
    else:
        raise AssertionError("expected a page-change diagnostic")
