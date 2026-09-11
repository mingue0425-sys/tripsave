from datetime import datetime, timezone

import pytest

from backend.fuel.models import FuelType
from backend.fuel.parser import (
    FuelParseError,
    FuelUnitUnsupportedError,
    parse_opinet_fuel_html,
    parse_price_krw_per_l,
)


LIQUID_HTML = """
<html><body>
<p>제품별 평균공급가격 (원/리터)</p>
<table id="numbox">
  <thead><tr><th>구분</th><th>고급휘발유</th><th>보통휘발유</th><th>자동차용경유</th></tr></thead>
  <tbody>
    <tr><td>2026년09월09일</td><td>2,100.00</td><td>1,850.10</td><td>1,840.20</td></tr>
    <tr><td>2026년09월10일</td><td>2,101.00</td><td>1,858.91</td><td>1,844.02</td></tr>
    <tr><td>전일대비</td><td>+1.00</td><td>+8.81</td><td>+3.82</td></tr>
  </tbody>
</table>
</body></html>
"""

LPG_HTML = """
<html><body>
<table id="tbody1">
  <caption>자동차충전소 평균 판매가격 (원/ℓ)</caption>
  <thead><tr><th>구분</th><th>자동차부탄</th></tr></thead>
  <tbody>
    <tr><td>2026년09월09일</td><td>1,095.50</td></tr>
    <tr><td>2026년09월10일</td><td>1,098.31</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_parser_selects_semantic_gasoline_and_diesel_columns() -> None:
    fetched_at = datetime(2026, 9, 11, tzinfo=timezone.utc)

    gasoline = parse_opinet_fuel_html(
        LIQUID_HTML,
        FuelType.GASOLINE,
        source_url="https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
        fetched_at=fetched_at,
    )
    diesel = parse_opinet_fuel_html(
        LIQUID_HTML,
        FuelType.DIESEL,
        source_url="https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
        fetched_at=fetched_at,
    )

    assert gasoline.price_krw_per_l == 1858.91
    assert diesel.price_krw_per_l == 1844.02
    assert gasoline.observed_at == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert gasoline.raw_evidence_hash and len(gasoline.raw_evidence_hash) == 64


def test_parser_selects_automotive_lpg_and_normalizes_litre_unit() -> None:
    result = parse_opinet_fuel_html(
        LPG_HTML,
        FuelType.LPG,
        source_url="https://www.opinet.co.kr/user/dopvsavsel/dopVsAvselSelect.do",
    )

    assert result.price_krw_per_l == 1098.31
    assert result.unit.value == "krw_per_l"
    assert result.observed_at == datetime(2026, 9, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("displayed", "expected"),
    [("1,700원", 1700.0), ("1,698.42원/리터", 1698.42), ("1,098.31원/ℓ", 1098.31)],
)
def test_price_parser_accepts_comma_decimal_and_display_unit(displayed: str, expected: float) -> None:
    assert parse_price_krw_per_l(displayed) == expected


def test_parser_rejects_missing_table_and_unsupported_unit() -> None:
    with pytest.raises(FuelParseError):
        parse_opinet_fuel_html(
            "<html><table><tr><th>다른 제품</th></tr></table></html>",
            FuelType.GASOLINE,
            source_url="https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
        )
    with pytest.raises(FuelUnitUnsupportedError):
        parse_opinet_fuel_html(
            LIQUID_HTML.replace("원/리터", "원/kg"),
            FuelType.GASOLINE,
            source_url="https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
        )


@pytest.mark.parametrize("value", ["0원", "-1원", "NaN", "Infinity", "가격없음"])
def test_price_parser_rejects_non_positive_or_non_finite(value: str) -> None:
    with pytest.raises(FuelParseError):
        parse_price_krw_per_l(value)
