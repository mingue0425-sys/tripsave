"""Booking.com public search-page source adapter.

Booking's public result page currently renders its result cards after normal
browser JavaScript runs.  This adapter keeps one Playwright browser alive and
reads the visible DOM; it does not call a Booking API, use a login, or bypass
CAPTCHA/access controls.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Iterable
from datetime import date
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from backend.accommodation.models import AccommodationResult
from backend.geo import haversine_distance_meters
from backend.models import Location
from backend.places import load_places
from crawler.accommodation.errors import (
    AccommodationAccessDeniedError,
    AccommodationDestinationError,
    AccommodationPageChangedError,
    AccommodationPriceUnavailableError,
    AccommodationSourceError,
    AccommodationSourceTimeoutError,
)
from crawler.accommodation.parser import parse_booking_search_html


LOGGER = logging.getLogger(__name__)

BOOKING_SOURCE = "booking"
BOOKING_SEARCH_URL = "https://www.booking.com/searchresults.html"
BOOKING_HOSTNAMES = frozenset({"booking.com", "www.booking.com"})
BOOKING_USER_AGENT_NOTE = "KoreaTripOptimizer/0.6 (public Booking.com HTML)"
_COORDINATE_LABEL_RE = re.compile(
    r"^\s*(-?[0-9]+(?:\.[0-9]+)?)\s*,\s*(-?[0-9]+(?:\.[0-9]+)?)\s*$"
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _ascii_aliases(values: Iterable[str]) -> list[str]:
    return [value.strip() for value in values if value.strip() and value.isascii()]


def _nearest_local_destination_label(destination: Location) -> str | None:
    """Resolve a map-click coordinate to the bundled city search label.

    This is a local label lookup, not geocoding.  It exists so a map click
    within a bundled city can use a regional public search page without
    inventing an address or accommodation coordinate.
    """

    nearest: tuple[float, str] | None = None
    for place in load_places():
        try:
            candidate = Location(
                lat=place.lat,
                lng=place.lng,
                label=place.name,
                source="local_index",
            )
            distance_m = haversine_distance_meters(destination, candidate)
        except (TypeError, ValueError):
            continue
        if distance_m > 80_000:
            continue
        labels = _ascii_aliases(place.aliases)
        label = labels[0] if labels else place.name.strip()
        if not label:
            continue
        if nearest is None or distance_m < nearest[0]:
            nearest = (distance_m, label)
    return nearest[1] if nearest else None


def canonical_booking_destination_label(destination: Location) -> str:
    """Return a source-search label without using an external geocoder."""

    raw_label = (destination.label or "").strip()
    if raw_label and not _COORDINATE_LABEL_RE.match(raw_label):
        return raw_label
    resolved = _nearest_local_destination_label(destination)
    if resolved:
        return resolved
    raise AccommodationDestinationError(
        "Booking requires a regional destination label; select a bundled city or enter its name."
    )


def build_booking_search_url(
    destination: Location,
    checkin: date,
    checkout: date,
    adults: int,
    children: int = 0,
) -> str:
    """Build a fixed-host public search URL with explicit stay parameters."""

    label = canonical_booking_destination_label(destination)
    query = urlencode(
        {
            "ss": label,
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
            "group_adults": adults,
            "no_rooms": 1,
            "group_children": children,
        }
    )
    return urlunsplit(("https", "www.booking.com", "/searchresults.html", query, ""))


class BookingComSource:
    """Browser-backed adapter for Booking's normal public search page."""

    source_name = BOOKING_SOURCE

    def __init__(
        self,
        *,
        navigation_timeout_s: float = 45.0,
        selector_timeout_s: float = 35.0,
        result_timeout_s: float = 20.0,
        request_interval_s: float = 1.5,
        headless: bool | None = None,
        executable_path: str | None = None,
    ) -> None:
        self.navigation_timeout_ms = max(1_000, int(navigation_timeout_s * 1_000))
        self.selector_timeout_ms = max(1_000, int(selector_timeout_s * 1_000))
        self.result_timeout_ms = max(1_000, int(result_timeout_s * 1_000))
        self.request_interval_s = max(0.0, float(request_interval_s))
        self.headless = (
            _env_bool("KTO_ACCOMMODATION_BROWSER_HEADLESS", False)
            if headless is None
            else bool(headless)
        )
        self.executable_path = executable_path or os.getenv(
            "KTO_ACCOMMODATION_BROWSER_EXECUTABLE_PATH"
        )
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self.last_request_events: list[dict[str, object]] = []

    async def _wait_between_requests(self) -> None:
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_request_at = time.monotonic() + self.request_interval_s

    async def _ensure_page(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            launch_kwargs: dict[str, object] = {"headless": self.headless}
            if self.executable_path:
                launch_kwargs["executable_path"] = self.executable_path
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._context = await self._browser.new_context(
                locale="en-US",
                timezone_id="Asia/Seoul",
            )
            self._page = await self._context.new_page()
            self._page.on("request", self._record_request)
            self._page.on("response", self._record_response)
            return self._page
        except AccommodationSourceError:
            raise
        except Exception as error:
            await self.close()
            raise AccommodationSourceError(
                "The public accommodation browser could not be started."
            ) from error

    def _record_request(self, request: Any) -> None:
        try:
            self.last_request_events.append(
                {"kind": "request", "method": request.method, "url": request.url}
            )
        except Exception:
            return

    def _record_response(self, response: Any) -> None:
        try:
            self.last_request_events.append(
                {
                    "kind": "response",
                    "method": response.request.method,
                    "url": response.url,
                    "status": response.status,
                }
            )
        except Exception:
            return

    @staticmethod
    def _response_status(response: Any) -> int | None:
        try:
            return int(response.status) if response is not None else None
        except (TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def _raise_for_response_status(status: int | None) -> None:
        if status in {401, 403, 429}:
            raise AccommodationAccessDeniedError(
                f"Booking denied the public search page (HTTP {status})."
            )
        if status is not None and status >= 400:
            raise AccommodationSourceError(
                f"Booking returned an HTTP error (HTTP {status})."
            )

    @staticmethod
    def _is_timeout(error: BaseException) -> bool:
        return "timeout" in type(error).__name__.casefold()

    async def _body_text(self, page: Any) -> str:
        try:
            return await page.locator("body").inner_text(timeout=self.result_timeout_ms)
        except Exception as error:
            if self._is_timeout(error):
                raise AccommodationSourceTimeoutError(
                    "The public accommodation result page timed out."
                ) from error
            return ""

    @staticmethod
    def _looks_access_denied(body_text: str) -> bool:
        normalized = body_text.casefold()
        return any(
            marker in normalized
            for marker in (
                "captcha",
                "verify you are human",
                "unusual traffic",
                "access denied",
                "robot check",
            )
        )

    @staticmethod
    def _looks_empty(body_text: str) -> bool:
        normalized = body_text.casefold()
        return any(
            marker in normalized
            for marker in (
                "no properties found",
                "no results",
                "we couldn't find",
                "숙소가 없습니다",
            )
        )

    async def search(
        self,
        destination: Location,
        checkin: date,
        checkout: date,
        adults: int,
        children: int = 0,
    ) -> list[AccommodationResult]:
        url = build_booking_search_url(
            destination,
            checkin,
            checkout,
            adults,
            children,
        )
        async with self._lock:
            await self._wait_between_requests()
            page = await self._ensure_page()
            self.last_request_events = []
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
            except Exception as error:
                if self._is_timeout(error):
                    raise AccommodationSourceTimeoutError(
                        "The public accommodation search page timed out while loading."
                    ) from error
                raise AccommodationSourceError(
                    "The public accommodation search page could not be loaded."
                ) from error

            status = self._response_status(response)
            self._raise_for_response_status(status)

            cards = page.locator('[data-testid="property-card"]').first
            try:
                await cards.wait_for(state="attached", timeout=self.selector_timeout_ms)
            except Exception as error:
                body_text = await self._body_text(page)
                if self._looks_access_denied(body_text):
                    raise AccommodationAccessDeniedError(
                        "Booking's public page requested an access verification."
                    ) from error
                if self._looks_empty(body_text):
                    return []
                if self._is_timeout(error):
                    raise AccommodationSourceTimeoutError(
                        "Booking did not render accommodation result cards in time."
                    ) from error
                raise AccommodationPageChangedError(
                    "Booking did not render the expected public result cards."
                ) from error

            try:
                html = await page.content()
                results = parse_booking_search_html(
                    html,
                    source_url=url,
                    checkin=checkin,
                    checkout=checkout,
                    adults=adults,
                    children=children,
                )
            except AccommodationSourceError:
                raise
            except Exception as error:
                raise AccommodationPageChangedError(
                    "Booking's public result markup could not be parsed."
                ) from error

            has_known_price_or_unavailability = any(
                offer.final_price_krw is not None or offer.availability is False
                for result in results
                for offer in result.offers
            )
            if not has_known_price_or_unavailability:
                raise AccommodationPriceUnavailableError(
                    "Booking returned accommodation cards without a usable price or availability state."
                )
            return results

    async def close(self) -> None:
        """Close the shared browser when the application shuts down/tests end."""

        page, context, browser, playwright = (
            self._page,
            self._context,
            self._browser,
            self._playwright,
        )
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        for resource in (page, context, browser):
            if resource is None:
                continue
            try:
                await resource.close()
            except Exception:
                LOGGER.debug("Could not close Booking browser resource", exc_info=True)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                LOGGER.debug("Could not stop Playwright", exc_info=True)
