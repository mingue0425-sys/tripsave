"""Local searchable city index.

The V0.1--V0.5 destination picker lives in this module so that it cannot be
shadowed by the V0.7 ``backend.places`` package.  The package keeps a small
backward-compatible re-export for callers that still import the old name.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from pathlib import Path

from backend.models import Place
from config import PLACES_DATA_FILE

LOGGER = logging.getLogger(__name__)


def normalize_query(value: str) -> str:
    """Normalize a local destination-search query."""

    return unicodedata.normalize("NFKC", value).strip().casefold()


def _read_places(places_file: Path = PLACES_DATA_FILE) -> list[dict[str, object]]:
    """Read the application-owned city index without making it fatal."""

    try:
        data = json.loads(places_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        LOGGER.warning("Local place index is unavailable; local search is empty.")
        return []
    except json.JSONDecodeError:
        LOGGER.warning("Local place index is invalid; local search is empty.")
        return []
    except OSError as error:
        LOGGER.warning("Could not read local place index: %s", error)
        return []

    return data if isinstance(data, list) else []


def load_places(places_file: Path = PLACES_DATA_FILE) -> list[Place]:
    """Load and validate the small application-owned searchable place index."""

    places: list[Place] = []
    for raw_place in _read_places(places_file):
        try:
            places.append(Place.model_validate(raw_place))
        except (TypeError, ValueError):
            continue
    return places


def search_places(query: str, limit: int = 8) -> list[Place]:
    """Return at most ``limit`` local places, ranked by exact/prefix match."""

    normalized = normalize_query(query)
    if not normalized:
        return []

    ranked: list[tuple[tuple[int, str], Place]] = []
    for place in load_places():
        candidates = [place.name, *place.aliases]
        candidate_values = [normalize_query(candidate) for candidate in candidates]
        if not any(normalized in candidate for candidate in candidate_values):
            continue
        if normalized == candidate_values[0]:
            rank = 0
        elif any(normalized == candidate for candidate in candidate_values[1:]):
            rank = 1
        elif candidate_values[0].startswith(normalized):
            rank = 2
        else:
            rank = 3
        ranked.append(((rank, place.name), place))

    ranked.sort(key=lambda item: item[0])
    return [place for _, place in ranked[:limit]]


__all__ = ["load_places", "normalize_query", "search_places"]
