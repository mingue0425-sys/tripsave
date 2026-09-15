"""Semantic HTML/JSON-LD parsers for the VisitKorea public pages."""

from __future__ import annotations

import json
import html as html_lib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse, urlunsplit

from bs4 import BeautifulSoup, Tag

from backend.places.models import PlaceCategory, PlaceRecord
from crawler.places.errors import SourceParseError, SourcePageChangedError


_COMPACT_NUMBER_RE = re.compile(r"^(?P<number>\d+(?:[.,]\d+)?)(?P<suffix>[KkMm])?$")
_RATING_RE = re.compile(
    r"(?<!\d)(?P<rating>\d{1,2}(?:[.,]\d+)?)\s*(?:/|of)\s*"
    r"(?P<scale>\d{1,2}(?:[.,]\d+)?)"
)
_BASED_RATING_RE = re.compile(
    r"(?:traveler|traveller)\s+rating[^\d]{0,100}"
    r"(?P<rating>\d{1,2}(?:[.,]\d+)?)"
    r"[^\d]{0,100}(?:based\s+on)\s*"
    r"(?P<reviews>[\d,.]+[KkMm]?)",
    re.IGNORECASE,
)
_REVIEW_RE = re.compile(
    r"(?:\(|\b|based\s+on\s+)"
    r"(?P<count>[\d,.]+[KkMm]?)\s*(?:reviews?|travelers?|travellers?)\b",
    re.IGNORECASE,
)
_COORDINATE_RE = re.compile(
    r"var\s+lat\s*=\s*(?P<lat>-?\d+(?:\.\d+)?)\s*;.*?"
    r"var\s+lot\s*=\s*(?P<lng>-?\d+(?:\.\d+)?)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_JSON_COORDINATE_RE = re.compile(
    r"[\"']latitude[\"']\s*:\s*[\"'](?P<lat>-?\d+(?:\.\d+)?)[\"'].*?"
    r"[\"']longitude[\"']\s*:\s*[\"'](?P<lng>-?\d+(?:\.\d+)?)[\"']",
    re.IGNORECASE | re.DOTALL,
)
_VISITKOREA_HOSTS = frozenset({"english.visitkorea.or.kr"})


@dataclass(frozen=True)
class PlaceCard:
    source_id: str
    name: str
    category: PlaceCategory
    source_url: str
    raw_category: str | None = None


def normalize_source_url(value: str, *, base_url: str) -> str | None:
    """Keep source links on the fixed VisitKorea HTTPS host."""

    parsed = urlparse(urljoin(base_url, value))
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in _VISITKOREA_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.path
    ):
        return None
    return urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(html_lib.unescape(value.replace("\xa0", " ")).split())
    return cleaned or None


def parse_compact_number(value: str | None) -> int | None:
    """Parse exact integers and common public-page K/M display notation."""

    cleaned = clean_text(value)
    if not cleaned:
        return None
    match = _COMPACT_NUMBER_RE.match(cleaned.replace(" ", ""))
    if not match:
        return None
    number = float(match.group("number").replace(",", ""))
    suffix = (match.group("suffix") or "").casefold()
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[suffix]
    return int(round(number * multiplier))


def parse_rating(value: str | None) -> tuple[float, float] | None:
    """Return ``(rating, scale)`` only when the source states both values."""

    cleaned = clean_text(value)
    if not cleaned:
        return None
    match = _RATING_RE.search(cleaned)
    if not match:
        return None
    rating = float(match.group("rating").replace(",", "."))
    scale = float(match.group("scale").replace(",", "."))
    if rating < 0 or scale <= 0 or rating > scale:
        return None
    return rating, scale


def parse_review_count(value: str | None) -> int | None:
    """Parse a review count without confusing dates or other numbers."""

    cleaned = clean_text(value)
    if not cleaned:
        return None
    match = _REVIEW_RE.search(cleaned)
    if not match:
        return None
    return parse_compact_number(match.group("count"))


def parse_coordinates(html: str) -> tuple[float, float] | None:
    """Parse source-provided coordinates; never derive them from an address."""

    match = _COORDINATE_RE.search(html) or _JSON_COORDINATE_RE.search(html)
    if not match:
        return None
    lat = float(match.group("lat"))
    lng = float(match.group("lng"))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def _json_ld_values(soup: BeautifulSoup) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            parsed = json.loads(script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                values.extend(item for item in graph if isinstance(item, dict))
            else:
                values.append(candidate)
    return values


def _first_json_ld(soup: BeautifulSoup) -> dict[str, Any]:
    values = _json_ld_values(soup)
    preferred_types = {
        "FoodEstablishment",
        "LocalBusiness",
        "Place",
        "Restaurant",
        "TouristAttraction",
        "TouristDestination",
    }
    for value in values:
        value_type = value.get("@type")
        if value_type in preferred_types or (
            isinstance(value_type, list) and preferred_types.intersection(value_type)
        ):
            return value
    for value in values:
        if value.get("address") or value.get("geo"):
            return value
    for value in values:
        if value.get("name"):
            return value
    return values[0] if values else {}


def _card_selector(category: PlaceCategory) -> str:
    return "#restaurantList > li" if category == PlaceCategory.RESTAURANT else "#attractionsList > li"


def parse_search_cards(
    html: str,
    category: PlaceCategory,
    *,
    base_url: str,
) -> list[PlaceCard]:
    """Parse category cards using semantic list IDs and stable vcontsId links."""

    soup = BeautifulSoup(html, "html.parser")
    list_selector = _card_selector(category).split(" > ")[0]
    container = soup.select_one(list_selector)
    if container is None:
        raise SourcePageChangedError(
            f"VisitKorea search page has no {list_selector} category container."
        )

    cards: list[PlaceCard] = []
    for item in soup.select(_card_selector(category)):
        anchor = item.select_one('a[href*="contentsView"]')
        if anchor is None:
            continue
        source_url = normalize_source_url(anchor.get("href", ""), base_url=base_url)
        if source_url is None:
            continue
        parsed_url = urlparse(source_url)
        params = parse_qs(parsed_url.query)
        source_ids = params.get("vcontsId", [])
        if not source_ids or not source_ids[0].strip():
            continue
        title_node = item.select_one(".title") or anchor
        name = clean_text(title_node.get_text(" ", strip=True))
        if not name:
            continue
        raw_node = item.select_one(".sup")
        raw_category = clean_text(raw_node.get_text(" ", strip=True)) if raw_node else None
        cards.append(
            PlaceCard(
                source_id=source_ids[0].strip(),
                name=name,
                category=category,
                source_url=source_url,
                raw_category=raw_category,
            )
        )

    if container.select("li") and not cards:
        raise SourcePageChangedError("VisitKorea search cards no longer expose stable content IDs.")
    return cards


def _info_value(soup: BeautifulSoup, labels: set[str]) -> str | None:
    for strong in soup.select(".info_text > strong"):
        label = clean_text(strong.get_text(" ", strip=True))
        if not label:
            continue
        label_lower = label.casefold()
        if not any(candidate.casefold() in label_lower for candidate in labels):
            continue
        parent = strong.find_parent(class_="info_text")
        if parent is None:
            continue
        values = [clean_text(node.get_text(" ", strip=True)) for node in parent.select("p")]
        values = [value for value in values if value]
        if values:
            return values[0]
    return None


def _address_from_json_ld(value: Any) -> str | None:
    if isinstance(value, str):
        return clean_text(value)
    if not isinstance(value, dict):
        return None
    street = clean_text(str(value.get("streetAddress", "")))
    if street:
        return street
    parts = [
        clean_text(str(value.get(key, "")))
        for key in ("postalCode", "addressLocality", "addressRegion")
    ]
    parts = [part for part in parts if part]
    return " ".join(parts) or None


def _json_ld_rating(value: dict[str, Any]) -> tuple[float | None, float | None, int | None]:
    aggregate = value.get("aggregateRating")
    if not isinstance(aggregate, dict):
        return None, None, None
    raw_rating = aggregate.get("ratingValue")
    raw_scale = aggregate.get("bestRating") or aggregate.get("ratingScale")
    try:
        rating = float(raw_rating) if raw_rating is not None else None
        scale = float(raw_scale) if raw_scale is not None else None
    except (TypeError, ValueError):
        rating, scale = None, None
    review_count = parse_compact_number(str(aggregate.get("reviewCount", "")))
    if rating is not None and scale is not None and rating > scale:
        rating, scale = None, None
    return rating, scale, review_count


def _visible_rating(soup: BeautifulSoup) -> tuple[float | None, float | None, int | None]:
    candidates: list[str] = []
    for node in soup.select('[class*="rating"], [class*="Rating"], [data-rating]'):
        text = clean_text(node.get_text(" ", strip=True))
        if text:
            candidates.append(text)
    candidates.extend(
        clean_text(str(node)) or ""
        for node in soup.find_all(string=re.compile(r"traveler\s+rating", re.IGNORECASE))
    )
    for candidate in candidates:
        parsed = parse_rating(candidate)
        if parsed:
            return parsed[0], parsed[1], parse_review_count(candidate)
        based = _BASED_RATING_RE.search(candidate)
        if based:
            return (
                float(based.group("rating").replace(",", ".")),
                None,
                parse_compact_number(based.group("reviews")),
            )
    return None, None, None


def _tags(value: dict[str, Any], soup: BeautifulSoup) -> list[str]:
    raw = value.get("touristType")
    tags: list[str] = []
    if isinstance(raw, str):
        tags.append(raw)
    elif isinstance(raw, list):
        tags.extend(str(item) for item in raw if item)
    for node in soup.select(".tag a, .tags a, [class*='tag'] a"):
        text = clean_text(node.get_text(" ", strip=True))
        if text and text not in tags:
            tags.append(text)
    return tags[:50]


def parse_place_detail(
    html: str,
    category: PlaceCategory,
    *,
    source: str,
    source_id: str,
    source_url: str,
    fetched_at: datetime,
    fallback_name: str | None = None,
    raw_category: str | None = None,
) -> PlaceRecord:
    """Parse a detail page with JSON-LD first and semantic visible fallbacks."""

    soup = BeautifulSoup(html, "html.parser")
    structured = _first_json_ld(soup)
    name = clean_text(str(structured.get("name", "")))
    if not name:
        heading = soup.select_one("h1")
        name = clean_text(heading.get_text(" ", strip=True)) if heading else None
    name = name or clean_text(fallback_name)
    if not name:
        raise SourceParseError("VisitKorea detail page has no stable place name.")

    address = _address_from_json_ld(structured.get("address"))
    address = address or _info_value(soup, {"address"})
    geo = structured.get("geo")
    coordinates: tuple[float, float] | None = None
    if isinstance(geo, dict):
        try:
            lat = float(geo.get("latitude"))
            lng = float(geo.get("longitude"))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                coordinates = lat, lng
        except (TypeError, ValueError):
            coordinates = None
    coordinates = coordinates or parse_coordinates(html)

    # A card name by itself is not enough to call a detail page valid.  This
    # guard turns a changed/verification page into parser diagnostics instead
    # of silently returning a misleading empty metadata record.
    if not structured and not address and coordinates is None:
        raise SourcePageChangedError("VisitKorea detail page has no semantic place metadata.")

    rating, rating_scale, review_count = _json_ld_rating(structured)
    if rating is None or review_count is None:
        visible_rating, visible_scale, visible_reviews = _visible_rating(soup)
        rating = rating if rating is not None else visible_rating
        rating_scale = rating_scale if rating_scale is not None else visible_scale
        review_count = review_count if review_count is not None else visible_reviews

    opening = structured.get("openingHoursSpecification")
    if isinstance(opening, list):
        opening = "; ".join(str(item) for item in opening if item)
    opening = clean_text(str(opening)) if opening else None
    opening = opening or _info_value(
        soup, {"operating hours", "days of operation", "hours of operation"}
    )

    tourist_types = structured.get("touristType")
    subcategory = None
    if isinstance(tourist_types, list) and tourist_types:
        subcategory = clean_text(str(tourist_types[0]))
    elif isinstance(tourist_types, str):
        subcategory = clean_text(tourist_types)

    lat, lng = coordinates if coordinates else (None, None)
    return PlaceRecord(
        source=source,
        source_id=source_id,
        source_url=source_url,
        name=name,
        category=category,
        lat=lat,
        lng=lng,
        address=address,
        rating=rating,
        rating_scale=rating_scale,
        review_count=review_count,
        fetched_at=fetched_at,
        subcategory=subcategory,
        raw_category=raw_category,
        opening_information=opening,
        tags=_tags(structured, soup),
    )
