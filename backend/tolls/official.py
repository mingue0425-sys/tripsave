"""Official Korea Expressway toll-page client and HTML parser.

Only the normal public HTML form is used: an initial GET establishes the
session and a POST submits ``zonename1``/``zonename2``.  The implementation
does not call the page's AJAX helper or any public data API.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_module
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from backend.tolls.models import TollVehicleClass
from backend.tolls.names import official_query_name
from config import (
    APP_VERSION,
    OFFICIAL_TOLL_URL,
    TOLL_CONNECT_TIMEOUT_S,
    TOLL_MAX_PRICE_KRW,
    TOLL_REQUEST_INTERVAL_S,
    TOLL_REQUEST_TIMEOUT_S,
    validate_official_toll_url,
)


PARSER_VERSION = "ex-usefee-html-v1"
CRAWLER_USER_AGENT = f"KoreaTripOptimizer/{APP_VERSION} (+local toll calculator)"
_PRICE_PATTERN = re.compile(r"(?<!\d)(\d[\d,\s]*)(?!\d)")
_DISTANCE_PATTERN = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*(?:km|㎞)", re.IGNORECASE)


class OfficialTollError(RuntimeError):
    """A source or parser failure that must not become a zero toll."""

    code = "TOLL_SOURCE_UNAVAILABLE"


class OfficialTollTimeoutError(OfficialTollError):
    code = "TOLL_SOURCE_TIMEOUT"


class OfficialTollAccessDeniedError(OfficialTollError):
    code = "TOLL_SOURCE_ACCESS_DENIED"


class OfficialTollParserError(OfficialTollError):
    code = "PARSER_ERROR"


class OfficialTollLookup(BaseModel):
    """Parsed authoritative result from the official HTML page."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: str = "official_web"
    entry_name: str = Field(min_length=1, max_length=200)
    exit_name: str = Field(min_length=1, max_length=200)
    route_label: str = Field(min_length=1, max_length=300)
    route_stops: list[str] = Field(default_factory=list, max_length=300)
    distance_km: float | None = Field(default=None, strict=True, ge=0)
    prices: dict[TollVehicleClass, int]
    source_url: str = OFFICIAL_TOLL_URL
    fetched_at: datetime
    raw_evidence_hash: str = Field(min_length=64, max_length=64)
    parser_version: str = PARSER_VERSION


def parse_price_krw(value: object, *, max_price_krw: int = TOLL_MAX_PRICE_KRW) -> int:
    """Parse a displayed won amount without accepting malformed values."""

    if not isinstance(value, str):
        raise OfficialTollParserError("A toll price cell is not text.")
    text = html_module.unescape(value).replace("\xa0", " ").strip()
    if re.search(r"-\s*\d", text):
        raise OfficialTollParserError(f"Could not parse a negative toll price: {text!r}")
    match = _PRICE_PATTERN.search(text)
    if not match:
        raise OfficialTollParserError(f"Could not parse a toll price: {text!r}")
    digits = re.sub(r"[^0-9]", "", match.group(1))
    if not digits:
        raise OfficialTollParserError(f"Could not parse a toll price: {text!r}")
    amount = int(digits)
    if amount < 0 or amount > max_price_krw:
        raise OfficialTollParserError(f"Toll price is outside the safe range: {amount}")
    return amount


def _cell_text(cell: Any) -> str:
    return " ".join(cell.stripped_strings)


def _vehicle_class_for_header(header: str) -> TollVehicleClass | None:
    compact = re.sub(r"\s+", "", header).casefold()
    if "경차" in compact:
        return TollVehicleClass.COMPACT
    mapping = {
        "1종": TollVehicleClass.CLASS_1,
        "2종": TollVehicleClass.CLASS_2,
        "3종": TollVehicleClass.CLASS_3,
        "4종": TollVehicleClass.CLASS_4,
        "5종": TollVehicleClass.CLASS_5,
    }
    for prefix, vehicle_class in mapping.items():
        if compact.startswith(prefix):
            return vehicle_class
    return None


def _extract_prices(container: Any) -> tuple[str, dict[TollVehicleClass, int]]:
    for table in container.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        header_classes = [_vehicle_class_for_header(_cell_text(cell)) for cell in header_cells]
        if len([value for value in header_classes if value is not None]) < 6:
            continue
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < len(header_classes):
                continue
            route_label = _cell_text(cells[0])
            if "~" not in route_label:
                continue
            prices: dict[TollVehicleClass, int] = {}
            for index, vehicle_class in enumerate(header_classes[1:], start=1):
                if vehicle_class is None:
                    continue
                prices[vehicle_class] = parse_price_krw(_cell_text(cells[index]))
            if len(prices) == 6:
                return route_label, prices
    raise OfficialTollParserError("The official page did not contain a complete toll table.")


def _extract_route_stops(container: Any) -> list[str]:
    for row in container.find_all("tr"):
        cells = row.find_all("td")
        if not cells or _cell_text(cells[0]) != "경로" or len(cells) < 2:
            continue
        text = _cell_text(cells[1])
        # The official page renders arrow images between stop names.  Their
        # alt text is not needed; keeping non-empty whitespace tokens is a
        # stable, human-readable evidence trail.
        return [part for part in re.split(r"\s+", text) if part]
    return []


def _extract_distance_km(container: Any) -> float | None:
    distance_element = container.find(id="range")
    if distance_element is None:
        return None
    match = _DISTANCE_PATTERN.search(_cell_text(distance_element))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_official_toll_html(
    document: str,
    *,
    requested_entry: str,
    requested_exit: str,
    source_url: str = OFFICIAL_TOLL_URL,
    fetched_at: datetime | None = None,
) -> OfficialTollLookup:
    """Parse the authoritative non-private route block (``#noMinja``)."""

    if not isinstance(document, str) or not document.strip():
        raise OfficialTollParserError("The official toll page was empty.")
    validate_official_toll_url(source_url)
    soup = BeautifulSoup(document, "html.parser")
    container = soup.find(id="noMinja")
    if container is None:
        raise OfficialTollParserError(
            "The official page did not return a Korea Expressway route block."
        )
    route_label, prices = _extract_prices(container)
    route_stops = _extract_route_stops(container)
    route_parts = [part.strip() for part in route_label.split("~", 1)]
    entry_name = route_parts[0] or official_query_name(requested_entry)
    exit_name = route_parts[-1] or official_query_name(requested_exit)
    if not entry_name or not exit_name:
        raise OfficialTollParserError("The official result did not identify both tollgates.")
    distance_km = _extract_distance_km(container)
    evidence = "|".join(
        [
            route_label,
            str(distance_km),
            "\x1f".join(route_stops),
            "|".join(f"{key.value}:{prices[key]}" for key in TollVehicleClass),
        ]
    )
    return OfficialTollLookup(
        entry_name=entry_name,
        exit_name=exit_name,
        route_label=route_label,
        route_stops=route_stops,
        distance_km=distance_km,
        prices=prices,
        source_url=source_url,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        raw_evidence_hash=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
    )


class KoreaExpresswayTollCrawler:
    """Rate-limited normal HTML client for the official toll page."""

    def __init__(
        self,
        *,
        source_url: str = OFFICIAL_TOLL_URL,
        request_timeout_s: float = TOLL_REQUEST_TIMEOUT_S,
        connect_timeout_s: float = TOLL_CONNECT_TIMEOUT_S,
        request_interval_s: float = TOLL_REQUEST_INTERVAL_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.source_url = validate_official_toll_url(source_url)
        self.timeout = httpx.Timeout(request_timeout_s, connect=connect_timeout_s)
        self.request_interval_s = max(0.0, request_interval_s)
        self.transport = transport
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._robots_checked = False

    async def _wait_for_rate_limit(self) -> None:
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_request_at = time.monotonic() + self.request_interval_s

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.status_code in {401, 403, 429}:
            raise OfficialTollAccessDeniedError(
                f"The official toll page denied automated access (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            raise OfficialTollError(
                f"The official toll page returned HTTP {response.status_code}."
            )

    def _robots_url(self) -> str:
        parsed = urlsplit(self.source_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))

    async def _ensure_robots_allowed(self, client: httpx.AsyncClient) -> None:
        """Check the source's current robots policy once per crawler instance."""

        if self._robots_checked:
            return
        await self._wait_for_rate_limit()
        response = await client.get(self._robots_url())
        # A missing robots file has no rules to apply. Other failures are
        # source failures, not permission to proceed blindly.
        if response.status_code == 404:
            self._robots_checked = True
            return
        self._check_response(response)
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        if not parser.can_fetch(CRAWLER_USER_AGENT, self.source_url):
            raise OfficialTollAccessDeniedError(
                "The official source robots policy does not allow this page."
            )
        self._robots_checked = True

    async def lookup(self, entry_name: str, exit_name: str) -> OfficialTollLookup:
        entry = official_query_name(entry_name)
        exit = official_query_name(exit_name)
        if not entry or not exit:
            raise OfficialTollParserError("Both official tollgate names are required.")
        async with self._lock:
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers={
                        "User-Agent": CRAWLER_USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml",
                    },
                    transport=self.transport,
                ) as client:
                    await self._ensure_robots_allowed(client)
                    await self._wait_for_rate_limit()
                    initial = await client.get(self.source_url)
                    self._check_response(initial)
                    await self._wait_for_rate_limit()
                    result = await client.post(
                        self.source_url,
                        # httpx's form encoder on this Windows/Python build
                        # can downgrade non-Latin strings to question marks.
                        # Encode the normal UTF-8 HTML form body explicitly.
                        content=urlencode(
                            {"zonename1": entry, "zonename2": exit},
                            encoding="utf-8",
                        ).encode("ascii"),
                        headers={
                            "Referer": self.source_url,
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )
                    self._check_response(result)
            except OfficialTollError:
                raise
            except httpx.TimeoutException as error:
                raise OfficialTollTimeoutError(
                    "The official toll page timed out."
                ) from error
            except httpx.RequestError as error:
                raise OfficialTollError(
                    "The official toll page could not be reached."
                ) from error
        return parse_official_toll_html(
            result.text,
            requested_entry=entry,
            requested_exit=exit,
            source_url=self.source_url,
        )
