"""Parser for the visible Booking.com public search-result DOM.

The parser consumes HTML only.  It does not call hidden JSON endpoints and it
does not infer a tax amount when the page does not display one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from backend.accommodation.models import (
    AccommodationOffer,
    AccommodationPriceBasis,
    AccommodationResult,
    PlaceRecord,
)
from crawler.accommodation.errors import AccommodationPageChangedError

PARSER_VERSION = "booking-search-dom-v2"
BOOKING_HOSTS = frozenset({"booking.com", "www.booking.com"})

_AMOUNT_PREFIX_RE = re.compile(
    r"(?:\bKRW\b|₩)\s*([0-9][0-9,\s]*(?:\.[0-9]+)?)", re.IGNORECASE
)
_AMOUNT_SUFFIX_RE = re.compile(
    r"([0-9][0-9,\s]*(?:\.[0-9]+)?)\s*(?:\bKRW\b|원)", re.IGNORECASE
)
_RATING_RE = re.compile(r"(?<![0-9])([0-9]{1,2}(?:[.,][0-9]+)?)(?![0-9])")
_REVIEW_RE = re.compile(r"([0-9][0-9,\.]*)\s+reviews?\b", re.IGNORECASE)
_DISTANCE_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*km\b", re.IGNORECASE)
_PROPERTY_PATH_RE = re.compile(r"^/hotel/([^/]+)/([^/?#]+)\.html$", re.IGNORECASE)
_NIGHTS_RE = re.compile(r"\b[0-9]+\s+nights?\b", re.IGNORECASE)
_PER_NIGHT_RE = re.compile(r"\bper\s+night\b|/\s*night\b", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedPrice:
    """Explicitly parsed price components; unknown fields remain ``None``."""

    base_price_krw: int | None
    taxes_krw: int | None
    final_price_krw: int | None


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(unescape(value).split())


def _amount_to_krw(value: str) -> int:
    normalized = value.replace(",", "").replace(" ", "")
    try:
        decimal_value = Decimal(normalized)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("The displayed KRW amount is not numeric.") from error
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise ValueError("The displayed KRW amount must be positive and finite.")
    if decimal_value != decimal_value.to_integral_value():
        raise ValueError("A KRW amount with a fractional won value is unsupported.")
    return int(decimal_value)


def parse_krw_amounts(value: str) -> list[int]:
    """Return displayed KRW amounts without accepting another currency."""

    text = _clean_text(value)
    matches = list(_AMOUNT_PREFIX_RE.finditer(text))
    matches.extend(_AMOUNT_SUFFIX_RE.finditer(text))
    # Prefix and suffix patterns can both match a string such as ``365,000
    # KRW``.  Keep DOM order and remove duplicates without changing values.
    amounts: list[int] = []
    spans: list[tuple[int, int]] = []
    for match in sorted(matches, key=lambda item: item.start()):
        try:
            amount = _amount_to_krw(match.group(1))
        except ValueError:
            continue
        if any(amount == existing and match.span() == span for existing, span in zip(amounts, spans)):
            continue
        amounts.append(amount)
        spans.append(match.span())
    return amounts


def parse_price_components(price_text: str, tax_text: str = "") -> ParsedPrice:
    """Parse the page's displayed price and tax wording.

    ``Includes taxes and fees`` produces only a final price because the page
    does not expose a numeric base/tax split.  An explicit ``+KRW ... taxes``
    amount is added only when both displayed components are present.
    """

    displayed = parse_krw_amounts(price_text)
    taxes_displayed = parse_krw_amounts(tax_text)
    if not displayed:
        return ParsedPrice(None, None, None)

    normalized_tax_text = _clean_text(tax_text).casefold()
    includes_taxes = any(
        marker in normalized_tax_text
        for marker in ("includes taxes", "including taxes", "taxes included", "세금 포함", "세금 및 봉사료 포함")
    )
    excludes_taxes = any(
        marker in normalized_tax_text
        for marker in ("excluding taxes", "taxes excluded", "세금 별도", "세금 불포함")
    )
    tax_language = any(
        marker in normalized_tax_text
        for marker in ("tax", "fee", "세금", "봉사료", "요금")
    )
    explicit_tax_amount = bool(taxes_displayed) and tax_language

    displayed_price = displayed[-1]
    if explicit_tax_amount and not includes_taxes and not excludes_taxes:
        taxes = taxes_displayed[-1]
        return ParsedPrice(displayed_price, taxes, displayed_price + taxes)
    if excludes_taxes:
        return ParsedPrice(displayed_price, None, None)
    if includes_taxes:
        return ParsedPrice(None, None, displayed_price)
    if tax_language:
        # The source mentions taxes/fees but does not show their amount or
        # inclusion.  Preserve the displayed amount as a base value only.
        return ParsedPrice(displayed_price, None, None)
    # A discount line may contain an original and a current price.  Neither is
    # a trustworthy base/tax split, so expose the current displayed amount as
    # final price only.
    return ParsedPrice(None, None, displayed_price)


def parse_rating_and_reviews(value: str) -> tuple[float | None, float | None, int | None]:
    """Parse Booking's score-out-of-10 and review count text."""

    text = _clean_text(value)
    rating_match = _RATING_RE.search(text)
    rating = None
    if rating_match:
        try:
            rating = float(rating_match.group(1).replace(",", "."))
        except ValueError:
            rating = None
    review_match = _REVIEW_RE.search(text)
    review_count = None
    if review_match:
        try:
            review_count = int(review_match.group(1).replace(",", "").replace(".", ""))
        except ValueError:
            review_count = None
    return rating, 10.0 if rating is not None else None, review_count


def parse_distance(value: str) -> float | None:
    match = _DISTANCE_RE.search(_clean_text(value))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_price_basis(value: str) -> AccommodationPriceBasis:
    """Infer a basis only from explicit visible Booking wording."""

    text = _clean_text(value)
    if _PER_NIGHT_RE.search(text):
        return AccommodationPriceBasis.PER_NIGHT
    if _NIGHTS_RE.search(text):
        return AccommodationPriceBasis.TOTAL_STAY
    return AccommodationPriceBasis.UNKNOWN


def normalize_property_url(href: str, *, request_url: str) -> str | None:
    """Keep only a validated Booking property URL and booking parameters."""

    absolute = urljoin(request_url, href)
    parsed = urlsplit(absolute)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in BOOKING_HOSTS
        or not _PROPERTY_PATH_RE.match(parsed.path)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    allowed_query_keys = {
        "checkin",
        "checkout",
        "group_adults",
        "req_adults",
        "no_rooms",
        "group_children",
        "req_children",
        "matching_block_id",
        "selected_currency",
    }
    query_values = parse_qs(parsed.query, keep_blank_values=False)
    query_pairs = [
        (key, value)
        for key in sorted(allowed_query_keys)
        for value in query_values.get(key, [])
    ]
    return urlunsplit(
        (
            "https",
            "www.booking.com",
            parsed.path,
            urlencode(query_pairs),
            "",
        )
    )


def source_id_from_url(value: str | None) -> str | None:
    if not value:
        return None
    match = _PROPERTY_PATH_RE.match(urlsplit(value).path)
    if not match:
        return None
    return f"{match.group(1).lower()}/{match.group(2).lower()}"


def _first_text(card: Tag, selector: str) -> str:
    element = card.select_one(selector)
    return _clean_text(element.get_text(" ", strip=True) if element else "")


def _card_has_no_availability(card_text: str) -> bool:
    normalized = card_text.casefold()
    return any(
        marker in normalized
        for marker in ("no availability", "sold out", "not available", "예약 불가", "매진")
    )


def parse_booking_search_html(
    html: str,
    *,
    source_url: str,
    checkin: date,
    checkout: date,
    adults: int,
    children: int,
    fetched_at: datetime | None = None,
) -> list[AccommodationResult]:
    """Parse visible property cards into source-preserved result records."""

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-testid="property-card"]')
    if not cards:
        raise AccommodationPageChangedError(
            "Booking search results did not contain property cards."
        )
    observed_at = fetched_at or datetime.now(timezone.utc)
    results: list[AccommodationResult] = []

    for card in cards:
        title_element = card.select_one('[data-testid="title"]')
        title = _clean_text(title_element.get_text(" ", strip=True) if title_element else "")
        title_link = card.select_one('[data-testid="title-link"]')
        href = title_link.get("href") if title_link else None
        property_url = normalize_property_url(str(href), request_url=source_url) if href else None
        source_id = source_id_from_url(property_url)
        if not title or not property_url or not source_id:
            continue

        address = _first_text(card, '[data-testid="address-link"]') or None
        rating_value = _first_text(card, '[data-testid="review-score"]')
        rating, rating_scale, review_count = parse_rating_and_reviews(rating_value)
        distance_node = card.select_one('[data-testid="distance"]')
        distance_text = (
            _clean_text(distance_node.get_text(" ", strip=True))
            if distance_node
            else None
        )
        distance_km = parse_distance(distance_text or "")

        duration_text = _first_text(card, '[data-testid="price-for-x-nights"]')
        price_basis = parse_price_basis(duration_text)

        price_text = _first_text(card, '[data-testid="price-and-discounted-price"]')
        tax_text = _first_text(card, '[data-testid="taxes-and-charges"]')
        if not price_text:
            # This fallback supports older Booking markup while remaining
            # scoped to the availability block rather than arbitrary card text.
            price_node = card.select_one('[data-testid="availability-rate-information"]')
            price_text = _clean_text(price_node.get_text(" ", strip=True) if price_node else "")
        try:
            price = parse_price_components(price_text, tax_text)
        except (TypeError, ValueError):
            price = ParsedPrice(None, None, None)

        room_name = (
            _first_text(card, '[data-testid="recommended-units"] h4')
            or _first_text(card, '[data-testid="room-name"]')
            or None
        )
        card_text = _clean_text(card.get_text(" ", strip=True))
        if price.final_price_krw is not None or price.base_price_krw is not None:
            availability: bool | None = True
        elif _card_has_no_availability(card_text):
            availability = False
        else:
            availability = None

        query_values = parse_qs(urlsplit(property_url).query)
        source_offer_id = next(
            (
                values[0]
                for key in ("matching_block_id", "all_sr_blocks")
                if (values := query_values.get(key))
            ),
            None,
        )
        place = PlaceRecord(
            source="booking",
            source_id=source_id,
            source_url=property_url,
            name=title,
            category="accommodation",
            # Booking's search cards do not expose coordinates in the visible
            # result DOM.  Do not geocode or manufacture a coordinate.
            lat=None,
            lng=None,
            address=address,
            rating=rating,
            rating_scale=rating_scale,
            review_count=review_count,
            fetched_at=observed_at,
        )
        offer = AccommodationOffer(
            source="booking",
            source_offer_id=source_offer_id,
            place_source_id=source_id,
            checkin=checkin,
            checkout=checkout,
            adults=adults,
            children=children,
            room_name=room_name,
            base_price_krw=price.base_price_krw,
            taxes_krw=price.taxes_krw,
            final_price_krw=price.final_price_krw,
            price_basis=price_basis,
            # A card without a numeric final price is still a valid raw place
            # and offer record.  Keep its price evidence unknown instead of
            # claiming a fresh price and making model validation discard it.
            price_freshness="fresh" if price.final_price_krw is not None else "unknown",
            availability=availability,
            fetched_at=observed_at,
        )
        results.append(
            AccommodationResult(
                place=place,
                offers=[offer],
                distance_km=distance_km,
                distance_text=distance_text,
            )
        )

    if not results:
        raise AccommodationPageChangedError(
            "Booking property cards changed and no valid source records were found."
        )
    return results
