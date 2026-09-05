from datetime import datetime, timezone

import pytest

from backend.tolls.official import (
    OfficialTollParserError,
    parse_official_toll_html,
    parse_price_krw,
)
from backend.tolls.models import TollVehicleClass


HTML_FRAGMENT = """
<div id="noMinja">
  <table>
    <tr><th>구분</th><th>1종</th><th>2종</th><th>3종</th><th>4종</th><th>5종</th><th>1종(경차)</th></tr>
    <tr><td>서울~부산</td><td>18,600원</td><td>19,000 원</td><td>19,700원</td><td>26,100원</td><td>30,700원</td><td>9,300원</td></tr>
  </table>
  <span id="range">385.8Km</span>
  <table><tr><td>경로</td><td>서울 <img alt="arrow"> 대전 <img alt="arrow"> 부산</td></tr></table>
</div>
"""


def test_official_parser_normalizes_all_vehicle_prices() -> None:
    result = parse_official_toll_html(
        HTML_FRAGMENT,
        requested_entry="서울",
        requested_exit="부산",
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert result.entry_name == "서울"
    assert result.exit_name == "부산"
    assert result.distance_km == 385.8
    assert result.prices[TollVehicleClass.CLASS_1] == 18_600
    assert result.prices[TollVehicleClass.CLASS_5] == 30_700
    assert result.prices[TollVehicleClass.COMPACT] == 9_300
    assert result.raw_evidence_hash and len(result.raw_evidence_hash) == 64


@pytest.mark.parametrize(
    ("text", "expected"),
    [("18,600원", 18_600), (" 9\xa0300 원 ", 9_300), ("0원", 0)],
)
def test_price_parser_handles_display_format(text: str, expected: int) -> None:
    assert parse_price_krw(text) == expected


def test_parser_rejects_incomplete_price_table() -> None:
    with pytest.raises(OfficialTollParserError):
        parse_official_toll_html(
            '<div id="noMinja"><table><tr><th>구분</th><th>1종</th></tr></table></div>',
            requested_entry="서울",
            requested_exit="부산",
        )


def test_price_parser_rejects_malformed_and_oversized_amounts() -> None:
    with pytest.raises(OfficialTollParserError):
        parse_price_krw("가격 미정")
    with pytest.raises(OfficialTollParserError):
        parse_price_krw("-1,000원")
    with pytest.raises(OfficialTollParserError):
        parse_price_krw("99,999,999원")
