"""Explicit failures for public accommodation source access and parsing."""

from __future__ import annotations


class AccommodationSourceError(RuntimeError):
    """Base class for failures that must not become a zero-price result."""

    code = "ACCOMMODATION_SOURCE_FAILED"


class AccommodationSourceTimeoutError(AccommodationSourceError):
    code = "ACCOMMODATION_SOURCE_TIMEOUT"


class AccommodationAccessDeniedError(AccommodationSourceError):
    code = "ACCOMMODATION_ACCESS_DENIED"


class AccommodationPageChangedError(AccommodationSourceError):
    code = "ACCOMMODATION_PAGE_CHANGED"


class AccommodationParseError(AccommodationSourceError):
    code = "ACCOMMODATION_PARSE_FAILED"


class AccommodationPriceUnavailableError(AccommodationSourceError):
    code = "ACCOMMODATION_PRICE_UNAVAILABLE"


class AccommodationDestinationError(AccommodationSourceError):
    code = "ACCOMMODATION_DESTINATION_LABEL_REQUIRED"
