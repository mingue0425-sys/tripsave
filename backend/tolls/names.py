"""Conservative toll-gate name normalization helpers."""

from __future__ import annotations

import re
import unicodedata


_SUFFIX_PATTERN = re.compile(
    r"(?:\s*(?:톨게이트|요금소|영업소|toll\s*gate|tollgate|tg))\s*$",
    re.IGNORECASE,
)
_NON_NAME_PATTERN = re.compile(r"[^0-9a-z가-힣]+", re.IGNORECASE)


def official_query_name(raw_name: str | None) -> str:
    """Return a conservative name suitable for the official form.

    Only a terminal gate suffix is removed.  This avoids turning unrelated
    names into the same official tollgate while handling common OSM labels such
    as ``서울TG`` and ``서울요금소``.
    """

    if not isinstance(raw_name, str):
        return ""
    value = unicodedata.normalize("NFKC", raw_name).strip()
    value = _SUFFIX_PATTERN.sub("", value).strip()
    return value


def normalize_toll_name(raw_name: str | None) -> str:
    value = official_query_name(raw_name).casefold()
    return _NON_NAME_PATTERN.sub("", value)
