"""Official Korea Expressway toll source adapters and result parser.

The public Korea Expressway page is a server-rendered form.  The current
page first validates station names through ``pathCheckN.do`` and then submits
the canonical names to ``selectUseFeeNList.do``.  The browser adapter below
performs that same flow with Playwright.  The HTTP adapter is retained as a
testable/opt-in fallback, but is not the production default.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_module
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.tolls.models import TollVehicleClass
from backend.tolls.names import normalize_toll_name, official_query_name
from backend.tolls.schema import connect_database, initialize_schema
from config import (
    APP_VERSION,
    OFFICIAL_TOLL_URL,
    TOLL_BROWSER_ARTIFACT_DIR,
    TOLL_BROWSER_HEADLESS,
    TOLL_BROWSER_NAVIGATION_TIMEOUT_S,
    TOLL_BROWSER_RESULT_TIMEOUT_S,
    TOLL_BROWSER_SELECTOR_TIMEOUT_S,
    TOLL_CONNECT_TIMEOUT_S,
    TOLL_MAX_PRICE_KRW,
    TOLL_REQUEST_INTERVAL_S,
    TOLL_REQUEST_TIMEOUT_S,
    validate_official_toll_url,
)


LOGGER = logging.getLogger(__name__)

PARSER_VERSION = "ex-usefee-route-list-v2"
CRAWLER_USER_AGENT = f"KoreaTripOptimizer/{APP_VERSION} (+local toll calculator)"
OFFICIAL_ZONE_POPUP_PATH = "/portal/usefee/selectZoneNPop.do"
# Observed in the official page's ``fn_useSearch`` handler: the public form
# accepts "남양주" but submits the canonical station label below.  Keep this
# one verified alias explicit rather than manufacturing a fuzzy alias set.
OBSERVED_OFFICIAL_STATION_ALIASES = {
    "남양주": "남양주(서울양양)",
    # Observed in the current local OSM extract on the Daejeon-Gwangju
    # route; the public station UI/pathCheck canonicalizes this Hi-Pass lane
    # feature to the official ``광주`` station.
    "광주 톨게이트 하이패스": "광주",
}
_PRICE_PATTERN = re.compile(r"(?<!\d)(\d[\d,\s]*)(?!\d)")
_DISTANCE_PATTERN = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*(?:km|㎞)", re.IGNORECASE)


class OfficialTollError(RuntimeError):
    """A source or parser failure that must not become a zero toll."""

    code = "OFFICIAL_REQUEST_FAILED"


class OfficialTollTimeoutError(OfficialTollError):
    code = "OFFICIAL_REQUEST_TIMEOUT"


class OfficialTollAccessDeniedError(OfficialTollError):
    code = "OFFICIAL_ACCESS_DENIED"


class OfficialTollPageChangedError(OfficialTollError):
    code = "OFFICIAL_PAGE_CHANGED"


class OfficialTollStationNotFoundError(OfficialTollError):
    code = "OFFICIAL_STATION_NOT_FOUND"


class OfficialTollParserError(OfficialTollError):
    code = "OFFICIAL_PARSE_FAILED"


class OfficialTollPriceNotFoundError(OfficialTollParserError):
    code = "PRICE_NOT_FOUND"


class TollLookupClient(Protocol):
    async def lookup(self, entry_name: str, exit_name: str) -> "OfficialTollLookup":
        ...


class OfficialStation(BaseModel):
    """One canonical station record observed from the official station UI."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    official_id: str | None = Field(default=None, max_length=80)
    official_name: str = Field(min_length=1, max_length=200)
    normalized_name: str = Field(default="", max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    road_code: str | None = Field(default=None, max_length=40)
    road_name: str | None = Field(default=None, max_length=200)
    lat: float | None = Field(default=None, strict=True, ge=-90, le=90)
    lng: float | None = Field(default=None, strict=True, ge=-180, le=180)
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_url: str = OFFICIAL_TOLL_URL

    def model_post_init(self, __context: Any) -> None:
        if not self.normalized_name:
            self.normalized_name = normalize_toll_name(self.official_name)
        if self.official_name not in self.aliases:
            self.aliases.insert(0, self.official_name)


class OfficialStationDirectory(BaseModel):
    """Canonical station names collected from the official popup."""

    model_config = ConfigDict(extra="forbid")

    stations: list[OfficialStation] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def by_normalized_name(self, value: str) -> list[OfficialStation]:
        normalized = normalize_toll_name(value)
        if not normalized:
            return []
        return [
            station
            for station in self.stations
            if station.normalized_name == normalized
            or any(normalize_toll_name(alias) == normalized for alias in station.aliases)
        ]


class OfficialStationStore:
    """Small persistent store for verified official station identities."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def load(self) -> list[OfficialStation]:
        if not self.database_path.is_file():
            return []
        connection = connect_database(str(self.database_path))
        try:
            rows = connection.execute(
                """
                SELECT official_id, official_name, normalized_name, aliases_json,
                       road_code, road_name, verified_at, source_url
                FROM official_stations
                WHERE official_id IS NOT NULL AND official_id <> ''
                """
            ).fetchall()
        except Exception as error:
            LOGGER.warning("Official station dictionary read failed: %s", error)
            return []
        finally:
            connection.close()
        result: list[OfficialStation] = []
        for row in rows:
            try:
                result.append(
                    OfficialStation(
                        official_id=row[0],
                        official_name=row[1],
                        normalized_name=row[2],
                        aliases=json.loads(row[3]),
                        road_code=row[4],
                        road_name=row[5],
                        verified_at=datetime.fromisoformat(row[6]),
                        source_url=row[7],
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    def put(self, station: OfficialStation) -> None:
        if not station.official_id:
            return
        connection = connect_database(str(self.database_path))
        try:
            initialize_schema(connection)
            connection.execute(
                """
                INSERT OR REPLACE INTO official_stations(
                    official_id, official_name, normalized_name, aliases_json,
                    road_code, road_name, verified_at, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    station.official_id,
                    station.official_name,
                    station.normalized_name,
                    json.dumps(station.aliases, ensure_ascii=False, sort_keys=True),
                    station.road_code,
                    station.road_name,
                    station.verified_at.isoformat(),
                    station.source_url,
                ),
            )
            connection.commit()
        finally:
            connection.close()


class OfficialTollLookup(BaseModel):
    """Parsed authoritative result from the official HTML page."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: str = "official_web"
    entry_name: str = Field(min_length=1, max_length=200)
    exit_name: str = Field(min_length=1, max_length=200)
    entry_official_id: str | None = Field(default=None, max_length=80)
    exit_official_id: str | None = Field(default=None, max_length=80)
    route_label: str = Field(min_length=1, max_length=300)
    route_stops: list[str] = Field(default_factory=list, max_length=300)
    distance_km: float | None = Field(default=None, strict=True, ge=0)
    prices: dict[TollVehicleClass, int]
    source_url: str = OFFICIAL_TOLL_URL
    fetched_at: datetime
    raw_evidence_hash: str = Field(min_length=64, max_length=64)
    parser_version: str = PARSER_VERSION

    @model_validator(mode="after")
    def validate_price_values(self) -> "OfficialTollLookup":
        if any(
            not isinstance(price, int)
            or isinstance(price, bool)
            or price < 0
            for price in self.prices.values()
        ):
            raise ValueError("Official toll prices must be non-negative integers.")
        return self


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
    if "경차" in compact or "경형" in compact:
        return TollVehicleClass.COMPACT
    match = re.match(r"([1-5])종", compact)
    if match:
        return TollVehicleClass(f"class_{match.group(1)}")
    return None


@dataclass(frozen=True)
class _ParsedRoute:
    route_label: str
    prices: dict[TollVehicleClass, int]
    route_stops: list[str]
    distance_km: float | None
    block_index: int


def _extract_distance_km(block: Any) -> float | None:
    for row in block.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        if "총거리" not in _cell_text(cells[0]):
            continue
        text = " ".join(_cell_text(cell) for cell in cells[1:])
        match = _DISTANCE_PATTERN.search(text)
        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except ValueError:
                return None
    distance_element = block.find(id=re.compile(r"^(range|minjaRange|minjaRange2)$"))
    if distance_element is not None:
        match = _DISTANCE_PATTERN.search(_cell_text(distance_element))
        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except ValueError:
                return None
    return None


def _extract_route_stops(block: Any) -> list[str]:
    for row in block.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells or _cell_text(cells[0]) != "경로" or len(cells) < 2:
            continue
        return [part for part in re.split(r"\s+", _cell_text(cells[1])) if part]
    return []


def _extract_route_candidates(container: Any) -> list[_ParsedRoute]:
    # ``parse_official_toll_html`` passes the routeList element itself.  The
    # old parser looked for a descendant with the same id, fell back to the
    # whole result container, and consequently attached route 1's distance
    # and stops to every row.  Keep the route blocks separate so the selected
    # entry~exit row carries its own route evidence.
    route_list = container if container.get("id") == "routeList" else container.find(id="routeList")
    if route_list is not None:
        blocks = [
            block
            for block in route_list.find_all("div", class_=lambda value: value and "manage_list" in value)
            if block.find("table") is not None
        ]
        if not blocks:
            blocks = [route_list]
    else:
        blocks = [container]

    candidates: list[_ParsedRoute] = []
    for block_index, block in enumerate(blocks):
        for table in block.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            header_cells = rows[0].find_all(["th", "td"], recursive=False)
            header_classes = [_vehicle_class_for_header(_cell_text(cell)) for cell in header_cells]
            if len({value for value in header_classes if value is not None}) < len(TollVehicleClass):
                continue
            for row in rows[1:]:
                cells = row.find_all("td", recursive=False)
                if len(cells) < len(header_classes):
                    continue
                route_label = _cell_text(cells[0])
                if "~" not in route_label:
                    continue
                prices: dict[TollVehicleClass, int] = {}
                for index, vehicle_class in enumerate(header_classes[1:], start=1):
                    if vehicle_class is None:
                        continue
                    try:
                        prices[vehicle_class] = parse_price_krw(_cell_text(cells[index]))
                    except OfficialTollParserError as error:
                        raise OfficialTollPriceNotFoundError(str(error)) from error
                if len(prices) != len(TollVehicleClass):
                    raise OfficialTollPriceNotFoundError(
                        "The official result did not contain every vehicle class."
                    )
                candidates.append(
                    _ParsedRoute(
                        route_label=route_label,
                        prices=prices,
                        route_stops=_extract_route_stops(block),
                        distance_km=_extract_distance_km(block),
                        block_index=block_index,
                    )
                )
    return candidates


def _route_endpoints(route_label: str) -> tuple[str, str]:
    parts = [part.strip() for part in route_label.split("~", 1)]
    if len(parts) != 2:
        return "", ""
    return normalize_toll_name(parts[0]), normalize_toll_name(parts[1])


def _choose_route_candidate(
    candidates: list[_ParsedRoute], requested_entry: str, requested_exit: str
) -> _ParsedRoute:
    entry = normalize_toll_name(requested_entry)
    exit = normalize_toll_name(requested_exit)
    exact = [candidate for candidate in candidates if _route_endpoints(candidate.route_label) == (entry, exit)]
    if exact:
        return exact[0]

    # Do not accept an entry~middle or middle~exit row as a full journey.  A
    # partial row can carry a plausible price while being the wrong amount.
    raise OfficialTollParserError(
        "The official result did not contain a complete requested entry/exit row."
    )


def parse_official_toll_html(
    document: str,
    *,
    requested_entry: str,
    requested_exit: str,
    source_url: str = OFFICIAL_TOLL_URL,
    fetched_at: datetime | None = None,
    entry_official_id: str | None = None,
    exit_official_id: str | None = None,
) -> OfficialTollLookup:
    """Parse the requested full route from the official result page."""

    if not isinstance(document, str) or not document.strip():
        raise OfficialTollParserError("The official toll page was empty.")
    validate_official_toll_url(source_url)
    soup = BeautifulSoup(document, "html.parser")
    container = soup.find(id="routeList") or soup.find(id="noMinja")
    if container is None:
        raise OfficialTollPageChangedError(
            "The official page did not contain the expected route result container."
        )
    candidates = _extract_route_candidates(container)
    if not candidates:
        raise OfficialTollParserError("The official page did not contain a complete toll table.")
    selected = _choose_route_candidate(candidates, requested_entry, requested_exit)
    route_parts = [part.strip() for part in selected.route_label.split("~", 1)]
    entry_name = route_parts[0] or official_query_name(requested_entry)
    exit_name = route_parts[-1] or official_query_name(requested_exit)
    if not entry_name or not exit_name:
        raise OfficialTollParserError("The official result did not identify both tollgates.")
    evidence = "|".join(
        [
            selected.route_label,
            str(selected.distance_km),
            "\x1f".join(selected.route_stops),
            "|".join(f"{key.value}:{selected.prices[key]}" for key in TollVehicleClass),
        ]
    )
    return OfficialTollLookup(
        entry_name=entry_name,
        exit_name=exit_name,
        entry_official_id=entry_official_id,
        exit_official_id=exit_official_id,
        route_label=selected.route_label,
        route_stops=selected.route_stops,
        distance_km=selected.distance_km,
        prices=selected.prices,
        source_url=source_url,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        raw_evidence_hash=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
    )


def _road_context_matches(gate: Any, station: OfficialStation) -> bool:
    gate_ref = str(getattr(gate, "ref", "") or "").strip().casefold()
    if gate_ref and station.road_code and gate_ref == station.road_code.casefold():
        return True
    gate_road = normalize_toll_name(getattr(gate, "road_name", None))
    station_road = normalize_toll_name(station.road_name)
    if not gate_road or not station_road:
        return False
    return (
        gate_road == station_road
        or gate_road in station_road
        or station_road in gate_road
        or SequenceMatcher(None, gate_road, station_road).ratio() >= 0.60
    )


def _coordinate_matches(gate: Any, station: OfficialStation) -> bool:
    if station.lat is None or station.lng is None:
        return False
    gate_lat = getattr(gate, "lat", None)
    gate_lng = getattr(gate, "lng", None)
    if gate_lat is None or gate_lng is None:
        return False
    return abs(float(gate_lat) - station.lat) <= 0.30 and abs(float(gate_lng) - station.lng) <= 0.40


def match_official_station(
    osm_name: str | None,
    directory: OfficialStationDirectory,
    *,
    gate: Any | None = None,
) -> OfficialStation:
    """Match an OSM name with conservative alias/road/coordinate rules."""

    normalized = normalize_toll_name(osm_name)
    if not normalized:
        raise OfficialTollStationNotFoundError("The OSM toll-gate name is empty.")
    if gate is not None:
        operator = normalize_toll_name(getattr(gate, "operator", None))
        if operator and not (
            "한국도로공사" in operator
            or "koreaexpresswaycorporation" in operator
        ):
            raise OfficialTollStationNotFoundError(
                f"The OSM toll-gate operator is not supported: {getattr(gate, 'operator', '')!r}."
            )
    exact = directory.by_normalized_name(normalized)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        validated = [
            station
            for station in exact
            if gate is not None and (_coordinate_matches(gate, station) or _road_context_matches(gate, station))
        ]
        if len(validated) == 1:
            return validated[0]
        raise OfficialTollStationNotFoundError(
            f"The official station match is ambiguous for {osm_name!r}."
        )

    scored: list[tuple[float, OfficialStation]] = []
    for station in directory.stations:
        similarity = SequenceMatcher(None, normalized, station.normalized_name).ratio()
        if similarity < 0.86:
            continue
        if gate is None or not (_coordinate_matches(gate, station) or _road_context_matches(gate, station)):
            continue
        scored.append((similarity, station))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08):
        raise OfficialTollStationNotFoundError(
            f"No unambiguous official station match for {osm_name!r}."
        )
    return scored[0][1]


class HttpTollClient:
    """Bounded normal HTML form client used only as an opt-in adapter."""

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
            raise OfficialTollError(f"The official toll page returned HTTP {response.status_code}.")

    def _robots_url(self) -> str:
        parsed = urlsplit(self.source_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))

    async def _ensure_robots_allowed(self, client: httpx.AsyncClient) -> None:
        if self._robots_checked:
            return
        await self._wait_for_rate_limit()
        response = await client.get(self._robots_url())
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
                        content=urlencode(
                            {"zonename1": entry, "zonename2": exit}, encoding="utf-8"
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
                raise OfficialTollTimeoutError("The official toll page timed out.") from error
            except httpx.RequestError as error:
                raise OfficialTollError("The official toll page could not be reached.") from error
        return parse_official_toll_html(
            result.text,
            requested_entry=entry,
            requested_exit=exit,
            source_url=self.source_url,
        )


class BrowserTollClient:
    """Playwright implementation of the normal public station/form flow."""

    def __init__(
        self,
        *,
        source_url: str = OFFICIAL_TOLL_URL,
        navigation_timeout_s: float = TOLL_BROWSER_NAVIGATION_TIMEOUT_S,
        selector_timeout_s: float = TOLL_BROWSER_SELECTOR_TIMEOUT_S,
        result_timeout_s: float = TOLL_BROWSER_RESULT_TIMEOUT_S,
        request_interval_s: float = TOLL_REQUEST_INTERVAL_S,
        headless: bool = TOLL_BROWSER_HEADLESS,
        executable_path: str | None = None,
        artifacts_dir: Path | None = TOLL_BROWSER_ARTIFACT_DIR,
        station_store: OfficialStationStore | None = None,
    ) -> None:
        self.source_url = validate_official_toll_url(source_url)
        self.navigation_timeout_s = navigation_timeout_s
        self.selector_timeout_s = selector_timeout_s
        self.result_timeout_s = result_timeout_s
        self.request_interval_s = max(0.0, request_interval_s)
        self.headless = headless
        self.executable_path = executable_path or os.getenv("KTO_TOLL_BROWSER_EXECUTABLE_PATH")
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None
        self.station_store = station_store
        stored = station_store.load() if station_store else []
        self.station_directory = OfficialStationDirectory(stations=stored)
        # The SQLite store contains only stations that have already been
        # verified in a lookup, not the authoritative 455-name popup list.
        # Always refresh the public directory before matching a new pair.
        self._directory_loaded = False
        self._stored_stations = stored
        self._lock = asyncio.Lock()
        self.last_request_events: list[dict[str, object]] = []
        self.last_station_directory_count = 0
        self._next_lookup_at = 0.0

    async def _wait_between_lookups(self) -> None:
        delay = self._next_lookup_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_lookup_at = time.monotonic() + self.request_interval_s

    @staticmethod
    async def _safe_close(target: Any, label: str, timeout_s: float = 5.0) -> None:
        """Bound browser cleanup so it cannot mask a completed lookup."""

        if target is None:
            return
        try:
            await asyncio.wait_for(target.close(), timeout=timeout_s)
        except Exception as error:
            LOGGER.warning("Could not close toll %s cleanly: %s", label, error)

    @staticmethod
    def _event_request(request: Any) -> dict[str, object]:
        try:
            post_data = request.post_data
        except Exception:
            post_data = None
        return {
            "kind": "request",
            "method": request.method,
            "url": request.url,
            "post_body": post_data,
        }

    def _attach_network(self, page: Any) -> None:
        def on_request(request: Any) -> None:
            event = self._event_request(request)
            self.last_request_events.append(event)
            LOGGER.info(
                "official toll request method=%s url=%s post_body=%s",
                event["method"],
                event["url"],
                event["post_body"],
            )

        def on_response(response: Any) -> None:
            try:
                headers = response.headers
            except Exception:
                headers = {}
            event = {
                "kind": "response",
                "method": response.request.method,
                "url": response.url,
                "status": response.status,
                "content_type": headers.get("content-type", ""),
                "redirect": headers.get("location"),
            }
            self.last_request_events.append(event)
            LOGGER.info(
                "official toll response status=%s method=%s url=%s content_type=%s redirect=%s",
                event["status"],
                event["method"],
                event["url"],
                event["content_type"],
                event["redirect"],
            )

        page.on("request", on_request)
        page.on("response", on_response)

    async def _capture(self, page: Any, name: str) -> None:
        if self.artifacts_dir is None:
            return
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(self.artifacts_dir / f"{name}.png"), full_page=True)
            content = await page.content()
            if name == "after-search":
                soup = BeautifulSoup(content, "html.parser")
                fragment = soup.find(id="routeList") or soup.find(id="noMinja")
                if fragment is not None:
                    (self.artifacts_dir / "result-fragment.html").write_text(
                        str(fragment), encoding="utf-8"
                    )
        except Exception as error:  # diagnostics must never mask the source failure
            LOGGER.warning("Could not save toll debug artifact %s: %s", name, error)

    @staticmethod
    def _check_browser_response(response: Any) -> None:
        if response is None:
            return
        status = response.status
        if status in {401, 403, 429}:
            raise OfficialTollAccessDeniedError(
                f"The official toll page denied browser access (HTTP {status})."
            )
        if status >= 400:
            raise OfficialTollError(f"The official toll page returned HTTP {status}.")

    async def _load_station_directory(self, context: Any) -> None:
        if self._directory_loaded:
            return
        popup = await context.new_page()
        self._attach_network(popup)
        try:
            response = await popup.goto(
                urljoin(self.source_url, OFFICIAL_ZONE_POPUP_PATH),
                wait_until="domcontentloaded",
                timeout=int(self.navigation_timeout_s * 1000),
            )
            self._check_browser_response(response)
            await popup.locator("form[name='frm1']").wait_for(
                state="visible", timeout=int(self.selector_timeout_s * 1000)
            )
            async with popup.expect_navigation(
                wait_until="domcontentloaded",
                timeout=int(self.navigation_timeout_s * 1000),
            ) as navigation_info:
                await popup.locator("form[name='frm1']").evaluate(
                    """(form) => {
                        form.elements.namedItem('name').value = '';
                        form.elements.namedItem('swd').value = '';
                        form.elements.namedItem('ewd').value = '';
                        form.elements.namedItem('roadCd').value = '';
                        form.elements.namedItem('routeClssCd').value = '';
                        form.elements.namedItem('pageViewYn').value = 'Y';
                        form.action = '/portal/usefee/selectZoneNPop.do';
                        HTMLFormElement.prototype.submit.call(form);
                    }"""
                )
            self._check_browser_response(await navigation_info.value)
            station_rows = popup.locator("#tabResult .highway_left")
            await station_rows.first.wait_for(
                state="attached", timeout=int(self.selector_timeout_s * 1000)
            )
            names = [
                value.strip()
                for value in await station_rows.all_text_contents()
                if value.strip()
            ]
            if not names:
                await self._capture(popup, "station-directory-failed")
                raise OfficialTollPageChangedError(
                    "The official station popup did not expose its station list."
                )
            unique_names = list(dict.fromkeys(names))
            stations = [OfficialStation(official_name=name, aliases=[name]) for name in unique_names]
            stored_by_name = {
                station.normalized_name: station for station in self._stored_stations
            }
            merged_stations: list[OfficialStation] = []
            for station in stations:
                stored_station = stored_by_name.get(station.normalized_name)
                if stored_station is None:
                    merged_stations.append(station)
                    continue
                merged_stations.append(
                    station.model_copy(
                        update={
                            "official_id": stored_station.official_id,
                            "aliases": list(
                                dict.fromkeys([*station.aliases, *stored_station.aliases])
                            ),
                            "road_code": stored_station.road_code,
                            "road_name": stored_station.road_name,
                        }
                    )
                )
            stations = merged_stations
            by_name = {station.official_name: station for station in stations}
            for alias, canonical_name in OBSERVED_OFFICIAL_STATION_ALIASES.items():
                station = by_name.get(canonical_name)
                if station is not None and alias not in station.aliases:
                    station.aliases.append(alias)
            self.station_directory = OfficialStationDirectory(stations=stations)
            self._directory_loaded = True
            self.last_station_directory_count = len(unique_names)
            LOGGER.info("official station dictionary collected count=%d", len(unique_names))
        except OfficialTollError:
            raise
        except Exception as error:
            await self._capture(popup, "station-directory-failed")
            if error.__class__.__name__.endswith("TimeoutError"):
                raise OfficialTollTimeoutError(
                    "The official station directory load timed out."
                ) from error
            if "execution context was destroyed" in str(error).casefold():
                raise OfficialTollPageChangedError(
                    "The official station popup navigated before its result was readable."
                ) from error
            raise OfficialTollError("The official station list could not be loaded.") from error
        finally:
            await self._safe_close(popup, "station popup")

    async def _path_check(
        self, page: Any, entry: str, exit: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            payload = await page.evaluate(
                """async ({entry, exit, timeoutMs}) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                        const response = await fetch('/portal/usefee/pathCheckN.do', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                            body: new URLSearchParams({zonename1: entry, zonename2: exit}),
                            signal: controller.signal,
                        });
                        return {status: response.status, text: await response.text()};
                    } finally {
                        clearTimeout(timer);
                    }
                }""",
                {"entry": entry, "exit": exit, "timeoutMs": int(self.result_timeout_s * 1000)},
            )
        except Exception as error:
            if "abort" in str(error).casefold() or "timeout" in str(error).casefold():
                raise OfficialTollTimeoutError("The official station validation timed out.") from error
            raise OfficialTollError("The official station validation request failed.") from error
        if not isinstance(payload, dict):
            raise OfficialTollPageChangedError(
                "The official station validation response was not an object."
            )
        try:
            status = int(payload.get("status", 0))
        except (TypeError, ValueError) as error:
            raise OfficialTollPageChangedError(
                "The official station validation status changed shape."
            ) from error
        if status in {401, 403, 429}:
            raise OfficialTollAccessDeniedError(
                f"The official station validation denied browser access (HTTP {status})."
            )
        if status >= 400:
            raise OfficialTollError(f"The official station validation returned HTTP {status}.")
        try:
            result = json.loads(payload.get("text", ""))
            entry_rows = result[0]
            exit_rows = result[1]
        except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
            raise OfficialTollPageChangedError(
                "The official station validation response changed shape."
            ) from error

        def choose(rows: Any, requested: str) -> dict[str, Any]:
            if not isinstance(rows, list):
                raise OfficialTollPageChangedError(
                    "The official station validation result is not a list."
                )
            normalized = normalize_toll_name(requested)
            exact = [
                row
                for row in rows
                if isinstance(row, dict)
                and normalize_toll_name(row.get("nosunNm")) == normalized
            ]
            if len(exact) != 1:
                raise OfficialTollStationNotFoundError(
                    f"The official station validation is ambiguous for {requested!r}."
                )
            return exact[0]

        return choose(entry_rows, entry), choose(exit_rows, exit)

    async def _resolve_stations(
        self,
        page: Any,
        entry_name: str,
        exit_name: str,
        *,
        entry_gate: Any | None = None,
        exit_gate: Any | None = None,
    ) -> tuple[OfficialStation, OfficialStation]:
        raw_entry_name = entry_name
        raw_exit_name = exit_name
        entry_query = official_query_name(entry_name)
        exit_query = official_query_name(exit_name)
        entry_match = match_official_station(
            entry_query, self.station_directory, gate=entry_gate
        )
        exit_match = match_official_station(
            exit_query, self.station_directory, gate=exit_gate
        )
        entry_row, exit_row = await self._path_check(
            page, entry_match.official_name, exit_match.official_name
        )

        def materialize(
            row: dict[str, Any], observed: OfficialStation, raw_name: str
        ) -> OfficialStation:
            official_name = str(row.get("nosunNm") or observed.official_name).strip()
            aliases = list(dict.fromkeys([*observed.aliases, raw_name, official_name]))
            station = OfficialStation(
                official_id=str(row.get("nosunCd") or "") or None,
                official_name=official_name,
                normalized_name=normalize_toll_name(official_name),
                aliases=aliases,
                road_code=str(row.get("roadCd") or "") or None,
                road_name=str(row.get("roadNm") or "") or None,
                source_url=self.source_url,
            )
            if station.official_id and self.station_store:
                try:
                    self.station_store.put(station)
                except (OSError, sqlite3.Error) as error:
                    # The identity store is a cache.  A read-only or locked
                    # local index must not discard an otherwise valid official
                    # result.
                    LOGGER.warning("Official station identity cache write failed: %s", error)
            return station

        return (
            materialize(entry_row, entry_match, raw_entry_name),
            materialize(exit_row, exit_match, raw_exit_name),
        )

    async def lookup(self, entry_name: str, exit_name: str) -> OfficialTollLookup:
        return await self._lookup(entry_name, exit_name)

    async def lookup_with_context(
        self,
        entry_name: str,
        exit_name: str,
        *,
        entry_gate: Any | None = None,
        exit_gate: Any | None = None,
    ) -> OfficialTollLookup:
        return await self._lookup(
            entry_name,
            exit_name,
            entry_gate=entry_gate,
            exit_gate=exit_gate,
        )

    async def _lookup(
        self,
        entry_name: str,
        exit_name: str,
        *,
        entry_gate: Any | None = None,
        exit_gate: Any | None = None,
    ) -> OfficialTollLookup:
        entry_query = official_query_name(entry_name)
        exit_query = official_query_name(exit_name)
        if not entry_query or not exit_query:
            raise OfficialTollStationNotFoundError("Both official tollgate names are required.")
        async with self._lock:
            await self._wait_between_lookups()
            try:
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError
                from playwright.async_api import async_playwright
            except ImportError as error:
                raise OfficialTollError(
                    "Playwright is required for the official browser toll source."
                ) from error
            self.last_request_events = []
            try:
                async with async_playwright() as playwright:
                    launch_options: dict[str, object] = {"headless": self.headless}
                    if self.executable_path:
                        launch_options["executable_path"] = self.executable_path
                    browser = await playwright.chromium.launch(**launch_options)
                    lookup_result: OfficialTollLookup | None = None
                    context = None
                    try:
                        context = await browser.new_context(
                            user_agent=CRAWLER_USER_AGENT,
                            locale="ko-KR",
                        )
                        page = await context.new_page()
                        self._attach_network(page)
                        response = await page.goto(
                            self.source_url,
                            wait_until="domcontentloaded",
                            timeout=int(self.navigation_timeout_s * 1000),
                        )
                        self._check_browser_response(response)
                        await page.locator("form[name='frm1']").wait_for(
                            state="attached", timeout=int(self.selector_timeout_s * 1000)
                        )
                        await self._capture(page, "before-search")
                        await self._load_station_directory(context)
                        entry_station, exit_station = await self._resolve_stations(
                            page,
                            entry_name,
                            exit_name,
                            entry_gate=entry_gate,
                            exit_gate=exit_gate,
                        )
                        async with page.expect_navigation(
                            wait_until="domcontentloaded",
                            timeout=int(self.navigation_timeout_s * 1000),
                        ) as navigation_info:
                            await page.locator("form[name='frm1']").evaluate(
                                """(form, values) => {
                                    for (const [name, value] of Object.entries(values)) {
                                        let input = form.elements.namedItem(name);
                                        if (!input) {
                                            input = document.createElement('input');
                                            input.name = name;
                                            form.appendChild(input);
                                        }
                                        input.value = value;
                                    }
                                    form.action = '/portal/usefee/selectUseFeeNList.do';
                                    HTMLFormElement.prototype.submit.call(form);
                                }""",
                                {
                                    "zonename1": entry_station.official_name,
                                    "zonename2": exit_station.official_name,
                                },
                            )
                        self._check_browser_response(await navigation_info.value)
                        try:
                            await page.locator("#routeList, #noMinja").first.wait_for(
                                state="attached", timeout=int(self.result_timeout_s * 1000)
                            )
                        except PlaywrightTimeoutError as error:
                            await self._capture(page, "after-search")
                            raise OfficialTollPageChangedError(
                                "The official result route DOM did not appear."
                            ) from error
                        await self._capture(page, "after-search")
                        document = await page.content()
                        lookup_result = parse_official_toll_html(
                            document,
                            requested_entry=entry_station.official_name,
                            requested_exit=exit_station.official_name,
                            source_url=self.source_url,
                            entry_official_id=entry_station.official_id,
                            exit_official_id=exit_station.official_id,
                        )
                    finally:
                        # Chromium occasionally keeps a network/page task
                        # alive after the result DOM is available.  Cleanup
                        # must not turn a successful lookup into a generic
                        # source failure or wait forever.
                        await self._safe_close(context, "toll context")
                        await self._safe_close(browser, "toll browser")
                    if lookup_result is None:
                        raise OfficialTollError("The official toll browser returned no result.")
                    return lookup_result
            except OfficialTollError:
                raise
            except Exception as error:
                if "timeout" in str(error).casefold() or error.__class__.__name__.endswith(
                    "TimeoutError"
                ):
                    raise OfficialTollTimeoutError(
                        "The official toll browser flow timed out."
                    ) from error
                raise OfficialTollError("The official toll browser flow failed.") from error


class KoreaExpresswayTollCrawler:
    """Facade preserving the V0.4 name while using BrowserTollClient first."""

    def __init__(
        self,
        *,
        source_url: str = OFFICIAL_TOLL_URL,
        request_timeout_s: float = TOLL_REQUEST_TIMEOUT_S,
        connect_timeout_s: float = TOLL_CONNECT_TIMEOUT_S,
        request_interval_s: float = TOLL_REQUEST_INTERVAL_S,
        transport: httpx.AsyncBaseTransport | None = None,
        browser_client: BrowserTollClient | None = None,
        http_client: HttpTollClient | None = None,
        prefer_http: bool = False,
        allow_http_fallback: bool = False,
        station_store: OfficialStationStore | None = None,
    ) -> None:
        self.source_url = validate_official_toll_url(source_url)
        self.http_client = http_client or HttpTollClient(
            source_url=self.source_url,
            request_timeout_s=request_timeout_s,
            connect_timeout_s=connect_timeout_s,
            request_interval_s=request_interval_s,
            transport=transport,
        )
        self.browser_client = browser_client or BrowserTollClient(
            source_url=self.source_url,
            request_interval_s=request_interval_s,
            station_store=station_store,
        )
        # A supplied MockTransport is the compatibility/test signal for the
        # old deterministic HTTP adapter.  Production defaults to browser.
        self.prefer_http = prefer_http or transport is not None
        self.allow_http_fallback = allow_http_fallback

    async def lookup(self, entry_name: str, exit_name: str) -> OfficialTollLookup:
        if self.prefer_http:
            return await self.http_client.lookup(entry_name, exit_name)
        try:
            return await self.browser_client.lookup(entry_name, exit_name)
        except OfficialTollError:
            if not self.allow_http_fallback:
                raise
            LOGGER.warning("Browser toll lookup failed; trying opt-in HTTP adapter")
            return await self.http_client.lookup(entry_name, exit_name)

    async def lookup_with_context(
        self,
        entry_name: str,
        exit_name: str,
        *,
        entry_gate: Any | None = None,
        exit_gate: Any | None = None,
    ) -> OfficialTollLookup:
        """Use OSM road/coordinate context for conservative station matching."""

        if self.prefer_http:
            return await self.http_client.lookup(entry_name, exit_name)
        try:
            return await self.browser_client.lookup_with_context(
                entry_name,
                exit_name,
                entry_gate=entry_gate,
                exit_gate=exit_gate,
            )
        except OfficialTollError:
            if not self.allow_http_fallback:
                raise
            LOGGER.warning("Browser toll lookup failed; trying opt-in HTTP adapter")
            return await self.http_client.lookup(entry_name, exit_name)
