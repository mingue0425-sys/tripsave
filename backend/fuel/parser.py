"""Semantic parser for the public Opinet average-price HTML tables."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup, Tag

from backend.fuel.models import FuelPriceResult, FuelType


PARSER_VERSION = "opinet-average-price-v1"
_FOUR_DIGIT_DATE = re.compile(r"(?P<year>20\d{2})\s*년\s*(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일")
_EIGHT_DIGIT_DATE = re.compile(r"(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})")
_TWO_DIGIT_DATE = re.compile(r"(?P<year>\d{2})\s*년\s*(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일")


class FuelParseError(RuntimeError):
    """Raised when the public result no longer has the expected meaning."""

    code = "FUEL_PARSE_FAILED"


class FuelUnitUnsupportedError(FuelParseError):
    code = "FUEL_UNIT_UNSUPPORTED"


def _text(value: Tag) -> str:
    return " ".join(value.get_text(" ", strip=True).split())


def _parse_date(value: str) -> datetime | None:
    for pattern in (_FOUR_DIGIT_DATE, _EIGHT_DIGIT_DATE, _TWO_DIGIT_DATE):
        match = pattern.search(value)
        if not match:
            continue
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        try:
            return datetime(
                year,
                int(match.group("month")),
                int(match.group("day")),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
    return None


def parse_price_krw_per_l(value: str) -> float:
    """Parse a displayed decimal price without accepting labels or zeros."""

    normalized = value.replace(",", "").replace("원", "").replace("/", "")
    normalized = normalized.replace("ℓ", "").replace("리터", "").strip()
    try:
        price = Decimal(normalized)
    except (InvalidOperation, ValueError) as error:
        raise FuelParseError(f"Fuel price is not numeric: {value!r}") from error
    if not price.is_finite() or price <= 0:
        raise FuelParseError(f"Fuel price must be positive: {value!r}")
    return float(price)


def _find_table(soup: BeautifulSoup, fuel_type: FuelType) -> tuple[Tag, list[str], int]:
    expected = {
        FuelType.GASOLINE: ("보통휘발유", "일반휘발유"),
        FuelType.DIESEL: ("자동차용경유",),
        FuelType.LPG: ("자동차부탄",),
    }[fuel_type]
    for table in soup.find_all("table"):
        # Use the row containing the semantic product label rather than a
        # global ``th`` index.  Some versions of the public page use ``th``
        # for a date row or a two-tier header; a global index would shift the
        # price column and could still produce a plausible but wrong number.
        for row in table.find_all("tr"):
            header_cells = row.find_all("th")
            if not header_cells:
                continue
            row_cells = row.find_all(["th", "td"])
            headers = [_text(cell) for cell in row_cells]
            for cell in header_cells:
                header = _text(cell)
                if any(label in header for label in expected):
                    return table, headers, row_cells.index(cell)
    raise FuelParseError(f"The Opinet result table has no {fuel_type.value} column.")


def _validate_unit(soup: BeautifulSoup, headers: list[str], fuel_type: FuelType) -> None:
    page_text = _text(soup)
    header_text = " ".join(headers)
    unit_text = f"{page_text} {header_text}"
    if "원/리터" in unit_text or "원/ℓ" in unit_text:
        return
    raise FuelUnitUnsupportedError(
        f"The Opinet {fuel_type.value} page did not identify a won-per-litre unit."
    )


def _latest_price_row(table: Tag, column_index: int) -> tuple[float, datetime]:
    rows: list[tuple[datetime, float]] = []
    body = table.find("tbody") or table
    for row in body.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) <= column_index:
            continue
        observed_at = _parse_date(_text(cells[0]))
        if observed_at is None:
            continue
        try:
            price = parse_price_krw_per_l(_text(cells[column_index]))
        except FuelParseError:
            continue
        rows.append((observed_at, price))
    if not rows:
        raise FuelParseError("The Opinet result has no dated positive price row.")
    observed_at, price = max(rows, key=lambda item: item[0])
    return price, observed_at


def parse_opinet_fuel_html(
    html: str,
    fuel_type: FuelType,
    *,
    source_url: str,
    fetched_at: datetime | None = None,
) -> FuelPriceResult:
    """Parse one current national-average price from the visible result table."""

    if not isinstance(html, str) or not html.strip():
        raise FuelParseError("The Opinet page was empty.")
    soup = BeautifulSoup(html, "html.parser")
    table, headers, column_index = _find_table(soup, fuel_type)
    _validate_unit(soup, headers, fuel_type)
    price, observed_at = _latest_price_row(table, column_index)
    fetched = fetched_at or datetime.now(timezone.utc)
    evidence = hashlib.sha256(str(table).encode("utf-8")).hexdigest()
    return FuelPriceResult(
        fuel_type=fuel_type,
        price_krw_per_l=price,
        source_url=source_url,
        observed_at=observed_at,
        fetched_at=fetched,
        source_status="fresh",
        complete=True,
        raw_evidence_hash=evidence,
        parser_version=PARSER_VERSION,
    )
