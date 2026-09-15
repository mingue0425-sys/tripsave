"""V0.7 restaurant/attraction models plus V0.5 compatibility exports."""

from backend.city_search import load_places, normalize_query, search_places


from .models import (  # noqa: E402  (keep legacy definitions above importable)
    PlaceCategory,
    PlaceDestination,
    PlaceRecord,
    PlaceSourceRecord,
    PlaceSearchIssue,
    PlaceSearchRequest,
    PlaceSearchResponse,
)


__all__ = [
    "PlaceCategory",
    "PlaceDestination",
    "PlaceRecord",
    "PlaceSourceRecord",
    "PlaceSearchIssue",
    "PlaceSearchRequest",
    "PlaceSearchResponse",
    "load_places",
    "normalize_query",
    "search_places",
]
