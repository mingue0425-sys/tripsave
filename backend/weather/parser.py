"""Semantic parser for the public KMA short-term forecast DOM.

The parser intentionally consumes the labels and data attributes that are
present in the page a visitor sees.  It does not inspect script payloads or
invent values for fields that the page leaves empty.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

from bs4 import BeautifulSoup, Tag

from .models import DailyWeather, WeatherStatus


PARSER_VERSION = "kma-web-v1"
_UNKNOWN_MARKERS = frozenset(
    {
        "",
        "-",
        "--",
        "—",
        "―",
        "자료없음",
        "자료 없음",
        "미정",
        "없음",
        "n/a",
        "na",
    }
)
_UNKNOWN_MARKERS_CASEFOLD = frozenset(marker.casefold() for marker in _UNKNOWN_MARKERS)
_TRACKED_FIELDS = (
    "condition",
    "temp_min_c",
    "temp_max_c",
    "precipitation_probability_pct",
    "precipitation",
    "wind_speed_mps",
)


class KmaWebParseError(ValueError):
    """Base class for a public-page parse failure."""

    code = "KMA_WEB_PARSE_FAILED"


class KmaWebPageChangedError(KmaWebParseError):
    """The critical semantic structure no longer matches the fixture."""

    code = "KMA_WEB_PAGE_CHANGED"


class KmaWebLocationMismatchError(KmaWebParseError):
    """The returned page does not identify the requested KMA area."""

    code = "KMA_WEB_LOCATION_NOT_FOUND"


@dataclass(frozen=True)
class PrecipitationValue:
    """A safe precipitation representation, including non-exact bounds."""

    mm: float | None
    min_mm: float | None
    max_mm: float | None
    text: str


@dataclass(frozen=True)
class KmaWebParseResult:
    days: list[DailyWeather]
    available_until: date | None
    field_count: int
    content_fingerprint: str
    parser_path: str = "html"


def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split())


def _is_unknown(value: str | None) -> bool:
    cleaned = _clean_text(value)
    return cleaned.casefold() in _UNKNOWN_MARKERS_CASEFOLD


def parse_temperature_text(value: str | None) -> float | None:
    """Parse Celsius values such as ``-3°`` and ``12℃`` only."""

    text = _clean_text(value).replace(",", "")
    if _is_unknown(text):
        return None
    match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*(?:℃|°C|°(?![A-Za-z]))",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    parsed = float(match.group(1))
    return parsed if math.isfinite(parsed) and -100.0 <= parsed <= 70.0 else None


def parse_probability_text(value: str | None) -> float | None:
    """Parse and range-check a visible percentage."""

    text = _clean_text(value).replace(",", "")
    if _is_unknown(text):
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
    if not match:
        return None
    parsed = float(match.group(1))
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 100.0:
        return None
    return parsed


def parse_wind_speed_text(value: str | None) -> float | None:
    """Parse a numeric wind speed and normalize it to metres per second."""

    text = _clean_text(value).replace(",", "")
    if _is_unknown(text):
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(m/s|km/h|㎞/h)", text, re.IGNORECASE)
    if not match:
        return None
    parsed = float(match.group(1))
    if not math.isfinite(parsed) or parsed < 0:
        return None
    if match.group(2).casefold() in {"km/h", "㎞/h"}:
        parsed /= 3.6
    return parsed


def parse_precipitation_text(value: str | None) -> PrecipitationValue | None:
    """Parse exact, range, and upper-bound precipitation without guessing.

    KMA uses ``~1mm`` for an amount below 1 mm.  That is kept as text and an
    upper bound; it is deliberately not converted to ``0`` or ``1`` mm.
    """

    text = _clean_text(value).replace(",", "")
    if _is_unknown(text):
        return None
    display_text = re.sub(r"^(?:예상\s*)?강수량\s*[:：]\s*", "", text)
    compact = text.replace(" ", "")
    number = r"\d+(?:\.\d+)?"

    range_match = re.search(
        rf"({number})\s*(?:~|〜|-|–|—)\s*({number})\s*mm",
        compact,
        re.IGNORECASE,
    )
    if range_match:
        lower = float(range_match.group(1))
        upper = float(range_match.group(2))
        if math.isfinite(lower) and math.isfinite(upper) and 0 <= lower <= upper:
            return PrecipitationValue(None, lower, upper, display_text)
        return None

    upper_match = re.search(
        rf"(?:~|＜|<)?\s*({number})\s*mm\s*(?:미만|이하)",
        text,
        re.IGNORECASE,
    )
    tilde_match = re.search(rf"~\s*({number})\s*mm", text, re.IGNORECASE)
    if upper_match or tilde_match:
        matched = upper_match or tilde_match
        assert matched is not None
        upper = float(matched.group(1))
        if math.isfinite(upper) and upper >= 0:
            return PrecipitationValue(None, None, upper, display_text)
        return None

    exact_match = re.search(rf"({number})\s*mm", text, re.IGNORECASE)
    if exact_match:
        exact = float(exact_match.group(1))
        if math.isfinite(exact) and exact >= 0:
            return PrecipitationValue(exact, exact, exact, display_text)
    return None


def normalize_condition(value: str | None) -> str | None:
    """Normalize common KMA wording while preserving unknown as ``None``."""

    text = _clean_text(value)
    if _is_unknown(text):
        return None
    compact = re.sub(r"\s+", "", text)
    if "소나기" in compact:
        return "소나기"
    if "비" in compact and "눈" in compact:
        return "비/눈"
    if "비" in compact or "빗방울" in compact:
        return "비"
    if "눈" in compact or "눈날림" in compact:
        return "눈"
    if "흐림" in compact:
        return "흐림"
    if "구름많" in compact:
        return "구름많음"
    if "구름조금" in compact:
        return "구름조금"
    if "맑음" in compact:
        return "맑음"
    return text or None


def _condition_text(node: Tag) -> str | None:
    text = _clean_text(node.get_text(" ", strip=True))
    title = _clean_text(node.get("title"))
    candidate = text if not _is_unknown(text) else title
    if "날씨" in candidate:
        candidate = candidate.rsplit("날씨", 1)[-1].strip()
    return candidate or None


def _combine_conditions(values: list[str]) -> tuple[str | None, str | None]:
    raw_values: list[str] = []
    normalized_values: list[str] = []
    for value in values:
        raw = _clean_text(value)
        normalized = normalize_condition(raw)
        if not raw or normalized is None:
            continue
        if raw not in raw_values:
            raw_values.append(raw)
        if normalized not in normalized_values:
            normalized_values.append(normalized)
    return (
        "/".join(raw_values) or None,
        "/".join(normalized_values) or None,
    )


def _temperature_by_label(container: Tag | None, label: str) -> float | None:
    if container is None:
        return None
    for node in container.find_all(["strong", "th", "dt", "span", "div", "p"]):
        label_text = _clean_text(node.get_text(" ", strip=True))
        if label not in label_text:
            continue
        parsed = parse_temperature_text(label_text)
        if parsed is not None:
            return parsed
        for sibling in node.find_all_next(["span", "strong", "td", "dd"], limit=4):
            parsed = parse_temperature_text(sibling.get_text(" ", strip=True))
            if parsed is not None:
                return parsed
    match = re.search(
        rf"{re.escape(label)}(?:기온)?\s*[:：]?\s*(-?\d+(?:\.\d+)?)\s*(?:℃|°C|°)",
        _clean_text(container.get_text(" ", strip=True)),
        re.IGNORECASE,
    )
    return float(match.group(1)) if match else None


def _hourly_rows(soup: BeautifulSoup) -> dict[date, list[Tag]]:
    grouped: dict[date, list[Tag]] = {}
    rows = soup.select("ul.item[data-date]")
    if rows:
        for row in rows:
            raw_date = row.get("data-date")
            try:
                row_date = date.fromisoformat(str(raw_date))
            except ValueError:
                continue
            grouped.setdefault(row_date, []).append(row)
        return grouped
    for daily in soup.select("div.daily[data-date]"):
        try:
            row_date = date.fromisoformat(str(daily.get("data-date")))
        except ValueError:
            continue
        grouped.setdefault(row_date, []).extend(daily.select("ul.item"))
    return grouped


def _label_node(row: Tag, label: str) -> Tag | None:
    for candidate in row.select("li"):
        if label in _clean_text(candidate.get_text(" ", strip=True)):
            return candidate
    return None


def _aggregate_precipitation(
    values: list[PrecipitationValue],
    *,
    unknown_texts: list[str] | None = None,
) -> PrecipitationValue | None:
    unknown_texts = unknown_texts or []
    if not values and not unknown_texts:
        return None
    unique_texts: list[str] = []
    for value in values:
        if value.text not in unique_texts:
            unique_texts.append(value.text)
    for value in unknown_texts:
        cleaned = _clean_text(value)
        if cleaned and cleaned not in unique_texts:
            unique_texts.append(cleaned)
    if unknown_texts:
        # A missing hourly cell can add an unknown amount to the daily total.
        # Keep the visible text for the UI, but never expose a partial sum or
        # bounds as if they described the complete day.
        return PrecipitationValue(None, None, None, " / ".join(unique_texts) or "확인 불가")
    if all(value.mm is not None for value in values):
        total = sum(value.mm or 0.0 for value in values)
        return PrecipitationValue(total, total, total, " / ".join(unique_texts))
    # The hourly public table may repeat the same bound.  Preserve an
    # envelope, never manufacture an exact daily amount from it.
    lower_values = [value.min_mm for value in values if value.min_mm is not None]
    upper_values = [value.max_mm for value in values if value.max_mm is not None]
    lower = min(lower_values) if len(lower_values) == len(values) else None
    upper = max(upper_values) if upper_values else None
    return PrecipitationValue(None, lower, upper, " / ".join(unique_texts))


def _location_name(soup: BeautifulSoup) -> str | None:
    for selector in (".dfs-location", ".location-name", "[data-location-name]"):
        node = soup.select_one(selector)
        if node is not None:
            text = _clean_text(node.get("data-location-name") or node.get_text(" ", strip=True))
            if text and not _is_unknown(text):
                return text[:200]
    return None


def _day_from_slide(
    slide: Tag,
    *,
    hourly_rows: list[Tag],
    source: str,
    source_url: str,
    fetched_at: Any,
    parser_version: str,
    content_fingerprint: str,
    location_name: str | None,
) -> DailyWeather:
    current = date.fromisoformat(str(slide.get("data-date")))
    min_temp = _temperature_by_label(slide.select_one(".daily-minmax"), "최저")
    max_temp = _temperature_by_label(slide.select_one(".daily-minmax"), "최고")

    condition_nodes = slide.select(
        ".daily-weather-am .wic, .daily-weather-pm .wic"
    ) or slide.select(".wic")
    raw_condition_values = [
        condition
        for node in condition_nodes
        if (condition := _condition_text(node)) is not None
    ]
    raw_condition, normalized_condition = _combine_conditions(raw_condition_values)

    probabilities = [
        parsed
        for node in slide.select(".daily-pop-am, .daily-pop-pm")
        if (parsed := parse_probability_text(node.get_text(" ", strip=True))) is not None
    ]
    winds: list[float] = []
    humidity: list[float] = []
    precipitation: list[PrecipitationValue] = []
    precipitation_unknown: list[str] = []
    wind_directions: list[str] = []
    hourly_conditions: list[str] = []
    for row in hourly_rows:
        if (node := row.select_one(".wic")) is not None:
            if (condition := _condition_text(node)) is not None:
                hourly_conditions.append(condition)
        if (node := _label_node(row, "강수확률")) is not None:
            if (parsed := parse_probability_text(node.get_text(" ", strip=True))) is not None:
                probabilities.append(parsed)
        precipitation_nodes = row.select("li.pcp")
        if not precipitation_nodes:
            if (node := _label_node(row, "강수량")) is not None:
                precipitation_nodes = [node]
        for node in precipitation_nodes:
            if (parsed := parse_precipitation_text(node.get_text(" ", strip=True))) is not None:
                precipitation.append(parsed)
            else:
                precipitation_unknown.append(node.get_text(" ", strip=True))
        if (node := _label_node(row, "바람")) is not None:
            if (parsed := parse_wind_speed_text(node.get_text(" ", strip=True))) is not None:
                winds.append(parsed)
            direction = node.select_one(".wdic")
            if direction is not None:
                direction_text = _clean_text(direction.get_text(" ", strip=True))
                if direction_text and direction_text not in wind_directions:
                    wind_directions.append(direction_text)
        if (node := _label_node(row, "습도")) is not None:
            text = node.get_text(" ", strip=True)
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
            if match:
                parsed = float(match.group(1))
                if math.isfinite(parsed) and 0 <= parsed <= 100:
                    humidity.append(parsed)

    if normalized_condition is None and hourly_conditions:
        raw_condition, normalized_condition = _combine_conditions(hourly_conditions)
    precip = _aggregate_precipitation(
        precipitation,
        unknown_texts=precipitation_unknown,
    )
    precip_known = precip is not None and not precipitation_unknown
    known_values = {
        "condition": normalized_condition,
        "temp_min_c": min_temp,
        "temp_max_c": max_temp,
        "precipitation_probability_pct": max(probabilities) if probabilities else None,
        "precipitation": precip_known,
        "wind_speed_mps": max(winds) if winds else None,
    }
    known_fields = [name for name in _TRACKED_FIELDS if known_values[name] is not None and known_values[name] is not False]
    missing_fields = [name for name in _TRACKED_FIELDS if name not in known_fields]
    completeness_pct = round(len(known_fields) / len(_TRACKED_FIELDS) * 100.0, 1)
    status = WeatherStatus.OK if not missing_fields else WeatherStatus.PARTIAL
    if min_temp is not None and max_temp is not None and min_temp > max_temp:
        raise KmaWebPageChangedError("KMA minimum temperature exceeds maximum temperature")
    return DailyWeather(
        date=current,
        condition=normalized_condition,
        raw_condition=raw_condition,
        normalized_condition=normalized_condition,
        temp_min_c=min_temp,
        temp_max_c=max_temp,
        precipitation_probability_pct=max(probabilities) if probabilities else None,
        precipitation_mm=precip.mm if precip_known and precip else None,
        precipitation_min_mm=precip.min_mm if precip_known and precip else None,
        precipitation_max_mm=precip.max_mm if precip_known and precip else None,
        precipitation_text=precip.text if precip else None,
        wind_speed_mps=max(winds) if winds else None,
        wind_direction="/".join(wind_directions) or None,
        humidity_pct=(sum(humidity) / len(humidity)) if humidity else None,
        source=source,
        source_url=source_url,
        parser_version=parser_version,
        content_fingerprint=content_fingerprint,
        location_name=location_name,
        known_fields=known_fields,
        missing_fields=missing_fields,
        completeness_pct=completeness_pct,
        fetched_at=fetched_at,
        status=status,
    )


def parse_kma_web_html(
    html: str,
    *,
    source: str = "kma_web",
    source_url: str,
    fetched_at: Any,
    expected_location_code: str | None = None,
    parser_version: str = PARSER_VERSION,
) -> KmaWebParseResult:
    """Parse the official KMA daily summary and visible hourly table."""

    if not isinstance(html, str) or not html.strip():
        raise KmaWebPageChangedError("KMA forecast HTML is empty")
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript, template"):
        node.decompose()
    slides = soup.select(".dfs-daily-slide[data-date]") or soup.select(
        "[data-date].dfs-daily-slide"
    )
    if not slides:
        raise KmaWebPageChangedError("KMA daily forecast structure is missing")
    if expected_location_code and expected_location_code not in str(soup):
        raise KmaWebLocationMismatchError("KMA forecast page has the wrong area code")

    parsed_dates: set[date] = set()
    for slide in slides:
        try:
            slide_date = date.fromisoformat(str(slide.get("data-date")))
        except ValueError as error:
            raise KmaWebPageChangedError("KMA forecast contains an invalid date") from error
        if slide_date in parsed_dates:
            raise KmaWebPageChangedError("KMA forecast contains duplicate dates")
        parsed_dates.add(slide_date)

    critical_signal = any(
        slide.select_one(".daily-minmax") is not None or slide.select_one(".wic") is not None
        for slide in slides
    )
    if not critical_signal:
        raise KmaWebPageChangedError("KMA critical forecast labels are missing")

    fingerprint = hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest()
    rows_by_date = _hourly_rows(soup)
    location_name = _location_name(soup)
    days: list[DailyWeather] = []
    for slide in slides:
        try:
            days.append(
                _day_from_slide(
                    slide,
                    hourly_rows=rows_by_date.get(
                        date.fromisoformat(str(slide.get("data-date"))),
                        [],
                    ),
                    source=source,
                    source_url=source_url,
                    fetched_at=fetched_at,
                    parser_version=parser_version,
                    content_fingerprint=fingerprint,
                    location_name=location_name,
                )
            )
        except KmaWebParseError:
            raise
        except (TypeError, ValueError) as error:
            # Pydantic/value failures from malformed page numbers must stay
            # inside the provider taxonomy instead of becoming a 500 response.
            raise KmaWebParseError("KMA forecast value validation failed") from error
    days.sort(key=lambda item: item.date)
    field_count = sum(len(day.known_fields) for day in days)
    return KmaWebParseResult(
        days=days,
        available_until=days[-1].date if days else None,
        field_count=field_count,
        content_fingerprint=fingerprint,
    )


__all__ = [
    "KmaWebLocationMismatchError",
    "KmaWebPageChangedError",
    "KmaWebParseError",
    "KmaWebParseResult",
    "PARSER_VERSION",
    "PrecipitationValue",
    "normalize_condition",
    "parse_kma_web_html",
    "parse_precipitation_text",
    "parse_probability_text",
    "parse_temperature_text",
    "parse_wind_speed_text",
]
