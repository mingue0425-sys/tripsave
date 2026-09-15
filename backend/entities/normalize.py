"""Conservative multilingual normalization helpers for V0.8 matching."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# This is a deliberately small, auditable bilingual vocabulary.  It makes
# common Korean/English renderings comparable without transliterating arbitrary
# names or deleting branch/location tokens.
_TOKEN_ALIASES = {
    "롯데호텔": ("lotte", "hotel"),
    "lottehotel": ("lotte", "hotel"),
    "롯데": ("lotte",),
    "lotte": ("lotte",),
    "호텔": ("hotel",),
    "hotel": ("hotel",),
    "서울": ("seoul",),
    "seoul": ("seoul",),
    "부산": ("busan",),
    "busan": ("busan",),
    "강릉": ("gangneung",),
    "gangneung": ("gangneung",),
    "경주": ("gyeongju",),
    "gyeongju": ("gyeongju",),
    "스타벅스": ("starbucks",),
    "starbucks": ("starbucks",),
    "mcdonalds": ("mcdonalds",),
    "맥도날드": ("mcdonalds",),
    "롯데리아": ("lotteria",),
    "lotteria": ("lotteria",),
    "메가커피": ("megacoffee",),
    "megacoffee": ("megacoffee",),
    "교촌치킨": ("kyochon", "chicken"),
    "kyochon": ("kyochon",),
    "역": ("station",),
    "station": ("station",),
    "타워": ("tower",),
    "tower": ("tower",),
    "서울타워": ("seoul", "tower"),
    "n서울타워": ("n", "seoul", "tower"),
    "부산타워": ("busan", "tower"),
    "박물관": ("museum",),
    "museum": ("museum",),
    "시장": ("market",),
    "market": ("market",),
    "점": ("branch",),
    "branch": ("branch",),
}

_REGION_ALIASES = {
    "서울특별시": "seoul",
    "서울": "seoul",
    "seoul": "seoul",
    "부산광역시": "busan",
    "부산": "busan",
    "busan": "busan",
    "강원특별자치도": "gangwon",
    "강원도": "gangwon",
    "강릉": "gangneung",
    "gangneung": "gangneung",
    "경상북도": "gyeongbuk",
    "경주": "gyeongju",
    "gyeongju": "gyeongju",
    "인천광역시": "incheon",
    "인천": "incheon",
    "incheon": "incheon",
    "대구광역시": "daegu",
    "대구": "daegu",
    "daegu": "daegu",
    "대전광역시": "daejeon",
    "대전": "daejeon",
    "daejeon": "daejeon",
    "광주광역시": "gwangju",
    "광주": "gwangju",
    "gwangju": "gwangju",
    "울산광역시": "ulsan",
    "울산": "ulsan",
    "ulsan": "ulsan",
    "제주특별자치도": "jeju",
    "제주": "jeju",
    "jeju": "jeju",
}

_CHAIN_ALIASES = {
    "starbucks",
    "mcdonalds",
    "lotteria",
    "megacoffee",
    "kyochon",
    "lotte",
}

_GENERIC_NAME_TOKENS = {"the", "hotel", "branch"}
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    """NFKC/casefold text while replacing punctuation with boundaries."""

    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    chars: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        chars.append(" " if category.startswith(("P", "S")) else char)
    return _WHITESPACE_RE.sub(" ", "".join(chars)).strip()


def _alias_tokens(tokens: Iterable[str]) -> list[str]:
    result: list[str] = []
    for token in tokens:
        result.extend(_TOKEN_ALIASES.get(token, (token,)))
    return result


def name_tokens(value: str | None) -> tuple[str, ...]:
    """Return semantic tokens while retaining branch, number, and location."""

    normalized = normalize_text(value)
    if not normalized:
        return ()
    return tuple(_alias_tokens(normalized.split()))


def normalize_name(value: str | None) -> str:
    return " ".join(name_tokens(value))


def address_tokens(value: str | None) -> tuple[str, ...]:
    normalized = normalize_text(value)
    if not normalized:
        return ()
    tokens: list[str] = []
    for token in normalized.split():
        tokens.append(_REGION_ALIASES.get(token, token))
    return tuple(tokens)


def normalize_address(value: str | None) -> str:
    return " ".join(address_tokens(value))


def region_keys(*values: str | None) -> frozenset[str]:
    """Find only explicit, known region aliases; no geocoding is performed."""

    found: set[str] = set()
    for value in values:
        for token in normalize_text(value).split():
            region = _REGION_ALIASES.get(token)
            if region:
                found.add(region)
    return frozenset(found)


def chain_tokens(value: str | None) -> frozenset[str]:
    return frozenset(token for token in name_tokens(value) if token in _CHAIN_ALIASES)


def branch_location_tokens(value: str | None) -> frozenset[str]:
    """Extract branch/location evidence without stripping it during matching."""

    return frozenset(
        token
        for token in name_tokens(value)
        if token not in _CHAIN_ALIASES and token not in _GENERIC_NAME_TOKENS
    )


def meaningful_name_tokens(value: str | None) -> tuple[str, ...]:
    return tuple(
        token
        for token in name_tokens(value)
        if token not in _GENERIC_NAME_TOKENS
    )


def has_hangul(value: str | None) -> bool:
    return any("HANGUL" in unicodedata.name(char, "") for char in (value or ""))


def coordinate_cell(lat: float, lng: float, cell_size_degrees: float = 0.005) -> tuple[int, int]:
    """Return a stable spatial blocking cell (~500m at Korean latitudes)."""

    return (int(lat // cell_size_degrees), int(lng // cell_size_degrees))


__all__ = [
    "address_tokens",
    "branch_location_tokens",
    "chain_tokens",
    "coordinate_cell",
    "has_hangul",
    "meaningful_name_tokens",
    "name_tokens",
    "normalize_address",
    "normalize_name",
    "normalize_text",
    "region_keys",
]
