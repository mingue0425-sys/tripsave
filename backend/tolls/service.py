"""Toll calculation orchestration from canonical route to TollResult."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from backend.async_lock import LoopLocalAsyncLock
from backend.geo import haversine_distance_meters
from backend.models import Location
from backend.routing.identity import make_route_id
from backend.routing.models import RouteRequest
from backend.tolls.cache import TollCacheError, TollRateCache
from backend.tolls.errors import (
    InvalidTollRequestError,
    TollIndexServiceUnavailableError,
)
from backend.tolls.index import TollIndex, TollIndexUnavailableError
from backend.tolls.models import (
    MatchedTollGate,
    OfficialStationReference,
    TollCalculationRequest,
    TollDiagnostics,
    TollFailureCode,
    TollJourney,
    TollResponse,
    TollResult,
    TollStage,
)
from backend.tolls.names import normalize_toll_name, official_query_name
from backend.tolls.official import (
    KoreaExpresswayTollCrawler,
    OBSERVED_OFFICIAL_STATION_ALIASES,
    OfficialStationStore,
    OfficialTollError,
    OfficialTollLookup,
    OfficialTollParserError,
    OfficialTollStationNotFoundError,
)
from config import (
    OFFICIAL_TOLL_URL,
    TOLL_CACHE_TTL_DAYS,
    TOLL_FAILURE_COOLDOWN_S,
    TOLL_BROWSER_WARMUP,
    TOLL_INDEX_DB,
    TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_M,
    TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_RATIO,
    TOLL_STALE_MAX_AGE_DAYS,
    TOLL_DEBUG_MODE,
)


LOGGER = logging.getLogger(__name__)


class TollCalculator:
    """Coordinate OSM evidence, official lookup, caching, and diagnostics."""

    def __init__(
        self,
        *,
        index_path: Path = TOLL_INDEX_DB,
        cache: TollRateCache | None = None,
        crawler: KoreaExpresswayTollCrawler | None = None,
    ) -> None:
        self.index = TollIndex(index_path)
        self.cache = cache or TollRateCache(
            index_path,
            ttl_days=TOLL_CACHE_TTL_DAYS,
            stale_max_age_days=TOLL_STALE_MAX_AGE_DAYS,
        )
        self.crawler = crawler or KoreaExpresswayTollCrawler(
            source_url=OFFICIAL_TOLL_URL,
            station_store=OfficialStationStore(index_path),
        )
        self._lookup_lock = LoopLocalAsyncLock()
        self._inflight_lock = LoopLocalAsyncLock()
        self._inflight: dict[str, asyncio.Task] = {}
        self._refresh_lock = LoopLocalAsyncLock()
        self._refresh_inflight: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._failure_cooldown: dict[str, tuple[float, str]] = {}
        self.metrics: Counter[str] = Counter()
        self._debug_by_route: dict[str, TollDiagnostics] = {}
        self.last_lookup_diagnostics: dict[str, object] = {}

    async def status(self) -> dict[str, object]:
        ready = self.index.ready
        result: dict[str, object] = {
            "status": "ready" if ready else "unavailable",
            "engine": "osm+tollgate-index+official-web-http-primary",
            "profile": "car",
            "local_index": ready,
            "official_source": OFFICIAL_TOLL_URL,
            "source_policy": "cache+verified-http+playwright-fallback",
            "cache_ttl_days": getattr(self.cache, "ttl", None).days
            if getattr(self.cache, "ttl", None) is not None
            else TOLL_CACHE_TTL_DAYS,
            "stale_max_age_days": getattr(self.cache, "stale_max_age", None).days
            if getattr(self.cache, "stale_max_age", None) is not None
            else TOLL_STALE_MAX_AGE_DAYS,
            "failure_cooldown_s": TOLL_FAILURE_COOLDOWN_S,
            "metrics": dict(self.metrics),
            "last_lookup": dict(self.last_lookup_diagnostics),
        }
        if not ready:
            result["message"] = (
                "Local OSM toll index is unavailable. "
                "Run scripts/build_tollgate_index.py."
            )
        return result

    async def close(self) -> None:
        """Cancel refresh work and release index/source resources."""

        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(self.crawler, "close", None)
        if close is not None:
            await close()
        close_index = getattr(self.index, "close", None)
        if close_index is not None:
            close_index()

    async def warmup(self) -> None:
        """Warm optional fallback resources without crawling an official page."""

        if not TOLL_BROWSER_WARMUP:
            return
        warmup = getattr(self.crawler, "warmup_browser", None)
        if warmup is not None:
            await warmup()

    def debug(self, route_id: str) -> dict[str, object] | None:
        diagnostics = self._debug_by_route.get(route_id)
        if diagnostics is None:
            return None
        value = diagnostics.model_dump(mode="json")
        value["route_id"] = route_id
        return value

    @staticmethod
    def _route_id(request: TollCalculationRequest) -> str:
        computed = make_route_id(request.origin, request.destination, request.route)
        if request.route_id and request.route_id != computed:
            raise InvalidTollRequestError("route_id does not match the route payload")
        if request.route.route_id and request.route.route_id != computed:
            raise InvalidTollRequestError("route route_id does not match its payload")
        return computed

    @staticmethod
    def _route_endpoint_is_near(location: Location, coordinate: list[float]) -> bool:
        if len(coordinate) != 2:
            return False
        try:
            endpoint = Location(lat=float(coordinate[1]), lng=float(coordinate[0]))
        except (TypeError, ValueError, ValidationError):
            return False
        return haversine_distance_meters(location, endpoint) <= 5_000.0

    @classmethod
    def _validate_route_binding(cls, request: TollCalculationRequest) -> None:
        try:
            RouteRequest(origin=request.origin, destination=request.destination)
        except ValidationError as error:
            raise InvalidTollRequestError("The toll request locations are invalid.") from error
        coordinates = request.route.geometry.coordinates
        if not cls._route_endpoint_is_near(request.origin, coordinates[0]):
            raise InvalidTollRequestError(
                "The route geometry is not bound to its requested origin."
            )
        if not cls._route_endpoint_is_near(request.destination, coordinates[-1]):
            raise InvalidTollRequestError(
                "The route geometry is not bound to its requested destination."
            )

    @staticmethod
    def _operator(analysis) -> str | None:
        operators = [road.operator for road in analysis.toll_roads if road.operator]
        operators.extend(
            gate.gate.operator for gate in analysis.gates if gate.gate.operator
        )
        if not operators:
            return None
        return Counter(operators).most_common(1)[0][0]

    @staticmethod
    def _advance(
        diagnostics: TollDiagnostics,
        stage: TollStage,
        *,
        raw_candidates: int | None = None,
        logical_gates: int | None = None,
        duplicate_groups: int | None = None,
    ) -> TollDiagnostics:
        completed = list(diagnostics.completed_stages)
        if stage not in completed:
            completed.append(stage)
        return diagnostics.model_copy(
            update={
                "stage": stage,
                "completed_stages": completed,
                **({"raw_candidates": raw_candidates} if raw_candidates is not None else {}),
                **({"logical_gates": logical_gates} if logical_gates is not None else {}),
                **(
                    {"duplicate_groups": duplicate_groups}
                    if duplicate_groups is not None
                    else {}
                ),
            }
        )

    def _store_debug(self, route_id: str, diagnostics: TollDiagnostics) -> None:
        self._debug_by_route[route_id] = diagnostics
        LOGGER.info(
            "Toll diagnostics route_id=%s stage=%s failure_stage=%s failure_code=%s "
            "raw_candidates=%d logical_gates=%d",
            route_id,
            diagnostics.stage.value,
            diagnostics.failure_stage.value if diagnostics.failure_stage else None,
            diagnostics.failure_code.value if diagnostics.failure_code else None,
            diagnostics.raw_candidates,
            diagnostics.logical_gates,
        )
        for detail in diagnostics.candidate_details:
            LOGGER.info(
                "Toll candidate detail route_id=%s #%s raw_osm_name=%r "
                "normalized_name=%r lat=%s lng=%s id=%s osm_id=%s "
                "osm_type=%s gate_type=%s ref=%r barrier=%r highway=%r toll=%r "
                "operator=%r road_name=%r route_distance_m=%s "
                "distance_to_route_m=%s position_along_route_m=%s "
                "duplicate_group=%s candidate_role=%s",
                route_id,
                detail.get("candidate_number"),
                detail.get("raw_osm_name"),
                detail.get("normalized_name"),
                detail.get("lat"),
                detail.get("lng"),
                detail.get("id"),
                detail.get("osm_id"),
                detail.get("osm_type"),
                detail.get("gate_type"),
                detail.get("ref"),
                detail.get("barrier"),
                detail.get("highway"),
                detail.get("toll"),
                detail.get("operator"),
                detail.get("road_name"),
                detail.get("route_distance_m"),
                detail.get("distance_to_route_m"),
                detail.get("position_along_route_m"),
                detail.get("duplicate_group"),
                detail.get("candidate_role"),
            )

    @staticmethod
    def _failure(
        diagnostics: TollDiagnostics,
        *,
        stage: TollStage,
        code: TollFailureCode,
    ) -> TollDiagnostics:
        """Record both the last successful lifecycle and the failed stage."""

        return diagnostics.model_copy(
            update={"failure_stage": stage, "failure_code": code}
        )

    @staticmethod
    def _partial_result(
        *,
        request: TollCalculationRequest,
        route_id: str,
        reason: str,
        detected_gates: list[MatchedTollGate],
        logical_gates: list[MatchedTollGate] | None = None,
        diagnostics: TollDiagnostics | None = None,
        known_toll_krw: int | None = None,
        unknown_segments: int = 1,
    ) -> TollResult:
        return TollResult(
            status="partial",
            complete=False,
            vehicle_class=request.vehicle_class,
            total_toll_krw=None,
            known_toll_krw=known_toll_krw,
            journeys=[],
            detected_toll_gates=detected_gates,
            logical_toll_gates=logical_gates or [],
            unknown_segments=unknown_segments,
            reason=reason,
            source_status="unavailable",
            route_id=route_id,
            diagnostics=diagnostics or TollDiagnostics(),
        )

    @staticmethod
    def _pair_names(
        pair: tuple[object, object], fallback_entry: str, fallback_exit: str
    ) -> tuple[str, str]:
        candidate_entry, candidate_exit = pair
        entry_gate = getattr(candidate_entry, "gate", candidate_entry)
        exit_gate = getattr(candidate_exit, "gate", candidate_exit)
        return (
            getattr(entry_gate, "name", None) or fallback_entry,
            getattr(exit_gate, "name", None) or fallback_exit,
        )

    def _pair_flight_key(
        self,
        entry_name: str,
        exit_name: str,
        pairs: list[tuple[object, object]],
    ) -> str:
        first_entry, first_exit = self._pair_names(pairs[0], entry_name, exit_name)
        entry_gate = getattr(pairs[0][0], "gate", pairs[0][0])
        exit_gate = getattr(pairs[0][1], "gate", pairs[0][1])
        entry_id = getattr(entry_gate, "official_id", None)
        exit_id = getattr(exit_gate, "official_id", None)
        if entry_id is None or exit_id is None:
            try:
                entry_id = entry_id or self.cache.resolve_official_id(first_entry)
                exit_id = exit_id or self.cache.resolve_official_id(first_exit)
            except TollCacheError:
                # A cache read failure must not turn a valid source lookup
                # into an error; normalized official names remain safe keys.
                pass
        return "|".join(
            [
                str(entry_id or normalize_toll_name(official_query_name(first_entry))),
                str(exit_id or normalize_toll_name(official_query_name(first_exit))),
            ]
        )

    async def _run_shared_lookup(self, key: str, operation):
        try:
            return await operation
        finally:
            current = asyncio.current_task()
            async with self._inflight_lock:
                if self._inflight.get(key) is current:
                    self._inflight.pop(key, None)

    async def _run_refresh_lookup(self, key: str, operation) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Background toll cache refresh failed key=%s", key)
        finally:
            current = asyncio.current_task()
            async with self._refresh_lock:
                if self._refresh_inflight.get(key) is current:
                    self._refresh_inflight.pop(key, None)

    def _track_background_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.exception()
            except Exception:
                LOGGER.exception("Background toll task inspection failed")

        task.add_done_callback(done)

    def _cooldown_failure_code(self, key: str) -> str | None:
        entry = self._failure_cooldown.get(key)
        if entry is None:
            return None
        expires_at, code = entry
        if expires_at > time.monotonic():
            self.metrics["failure_cooldown_hit"] += 1
            return code
        self._failure_cooldown.pop(key, None)
        return None

    async def _schedule_refresh(
        self,
        key: str,
        entry_name: str,
        exit_name: str,
        *,
        entry_gate,
        exit_gate,
        candidate_pairs: list[tuple[object, object]],
        route_distance_m: float | None = None,
    ) -> None:
        async with self._refresh_lock:
            if key in self._refresh_inflight:
                return
            task = asyncio.create_task(
                self._run_refresh_lookup(
                    key,
                    self._lookup_with_cache_uncached(
                        entry_name,
                        exit_name,
                        entry_gate=entry_gate,
                        exit_gate=exit_gate,
                        candidate_pairs=candidate_pairs,
                        flight_key=key,
                        route_distance_m=route_distance_m,
                        allow_stale_result=False,
                    ),
                )
            )
            self._refresh_inflight[key] = task
            self._track_background_task(task)

    async def _lookup_with_cache_uncached(
        self,
        entry_name: str,
        exit_name: str,
        *,
        entry_gate=None,
        exit_gate=None,
        candidate_pairs: list[tuple[object, object]] | None = None,
        flight_key: str,
        route_distance_m: float | None = None,
        allow_stale_result: bool = True,
    ) -> tuple[
        OfficialTollLookup | None,
        str,
        datetime | None,
        str | None,
        str,
        object | None,
        object | None,
    ]:
        pairs = candidate_pairs or [(entry_gate, exit_gate)]

        async def cached_candidate(
            *, allow_stale: bool,
        ) -> tuple[
            OfficialTollLookup | None,
            str,
            datetime | None,
            object | None,
            object | None,
        ]:
            pair_names = [
                self._pair_names(pair, entry_name, exit_name) for pair in pairs
            ]
            get_many = getattr(self.cache, "get_many", None)
            if get_many is not None:
                try:
                    cached_values = get_many(pair_names, allow_stale=allow_stale)
                except TollCacheError as error:
                    LOGGER.warning("Toll cache batch read failed: %s", error)
                    cached_values = [None] * len(pairs)
            else:
                # Keep small test doubles and older integrations compatible
                # with the original one-pair cache protocol.
                cached_values = []
                for candidate_entry_name, candidate_exit_name in pair_names:
                    try:
                        cached_values.append(
                            self.cache.get(
                                candidate_entry_name,
                                candidate_exit_name,
                                allow_stale=allow_stale,
                            )
                        )
                    except TollCacheError as error:
                        LOGGER.warning("Toll cache read failed: %s", error)
                        cached_values.append(None)

            for pair, cached in zip(pairs, cached_values):
                if cached is not None:
                    if (
                        route_distance_m is not None
                        and not self._distance_is_consistent(
                            route_distance_m, cached.lookup
                        )
                    ):
                        # A name/alias cache hit is not enough evidence for a
                        # route.  Do not reuse a valid but shorter/different
                        # official pair for this journey direction.
                        self.metrics["cache_rejected_distance_mismatch"] += 1
                        continue
                    return (
                        cached.lookup,
                        "fresh" if cached.fresh else "stale",
                        cached.lookup.fetched_at,
                        pair[0],
                        pair[1],
                    )
            return None, "unavailable", None, None, None

        cached_lookup, cached_status, cached_fetched_at, cached_entry, cached_exit = (
            await cached_candidate(allow_stale=False)
        )
        if cached_lookup is not None:
            self.metrics["cache_hit"] += 1
            return (
                cached_lookup,
                cached_status,
                cached_fetched_at,
                None,
                "CACHE",
                cached_entry,
                cached_exit,
            )

        self.metrics["cache_miss"] += 1
        stale_lookup, stale_status, stale_fetched_at, stale_entry, stale_exit = (
            await cached_candidate(allow_stale=True)
        )
        if stale_lookup is not None and allow_stale_result:
            self.metrics["stale_cache_used"] += 1
            self.metrics["stale_refresh_scheduled"] += 1
            await self._schedule_refresh(
                flight_key,
                entry_name,
                exit_name,
                entry_gate=entry_gate,
                exit_gate=exit_gate,
                candidate_pairs=pairs,
                route_distance_m=route_distance_m,
            )
            return (
                stale_lookup,
                stale_status,
                stale_fetched_at,
                None,
                "STALE_CACHE",
                stale_entry,
                stale_exit,
            )

        # A bounded negative result is deliberately different from a toll
        # cache entry: it contains no amount and therefore cannot turn an
        # unknown segment into free travel.  It only suppresses repeated
        # HTTP+browser attempts while a transient/unsupported pair cools down.
        cooldown_code = self._cooldown_failure_code(flight_key)
        if cooldown_code is not None:
            return None, "unavailable", None, cooldown_code, "UNAVAILABLE", None, None

        async with self._lookup_lock:
            fresh_lookup, fresh_status, fresh_fetched_at, fresh_entry, fresh_exit = (
                await cached_candidate(allow_stale=False)
            )
            if fresh_lookup is not None:
                self.metrics["cache_hit_after_lock"] += 1
                return (
                    fresh_lookup,
                    fresh_status,
                    fresh_fetched_at,
                    None,
                    "CACHE",
                    fresh_entry,
                    fresh_exit,
                )

            last_error: OfficialTollError | None = None
            for pair in pairs:
                candidate_entry_name, candidate_exit_name = self._pair_names(
                    pair, entry_name, exit_name
                )
                candidate_entry_gate = getattr(pair[0], "gate", pair[0])
                candidate_exit_gate = getattr(pair[1], "gate", pair[1])
                try:
                    lookup_with_context = getattr(self.crawler, "lookup_with_context", None)
                    if lookup_with_context is not None:
                        lookup = await lookup_with_context(
                            candidate_entry_name,
                            candidate_exit_name,
                            entry_gate=candidate_entry_gate,
                            exit_gate=candidate_exit_gate,
                        )
                    else:
                        lookup = await self.crawler.lookup(
                            candidate_entry_name,
                            candidate_exit_name
                        )
                    if (
                        route_distance_m is not None
                        and not self._distance_is_consistent(route_distance_m, lookup)
                    ):
                        # The official site can return a valid short sub-route
                        # for an early OSM candidate.  It is not the requested
                        # journey; continue with the next route-progress pair
                        # instead of caching or exposing that amount as the
                        # full-route toll.
                        self.metrics["source_rejected_distance_mismatch"] += 1
                        last_error = OfficialTollParserError(
                            "The official result distance did not match the requested route."
                        )
                        continue
                    try:
                        # Persist the canonical official names plus IDs.  The
                        # returned IDs remain the cache identity even when the
                        # OSM candidate used for the successful attempt is an
                        # alias such as ``하남요금소``.
                        self.cache.put(
                            lookup.entry_name,
                            lookup.exit_name,
                            lookup,
                            entry_official_id=lookup.entry_official_id,
                            exit_official_id=lookup.exit_official_id,
                        )
                    except TollCacheError as error:
                        LOGGER.warning("Toll cache write failed: %s", error)
                    source_path = getattr(self.crawler, "last_source_path", "HTTP")
                    if source_path not in {"HTTP", "PLAYWRIGHT"}:
                        source_path = "HTTP"
                    self.metrics[f"source_{source_path.casefold()}_success"] += 1
                    self.metrics["crawl_success"] += 1
                    self._failure_cooldown.pop(flight_key, None)
                    return (
                        lookup,
                        "fresh",
                        lookup.fetched_at,
                        None,
                        source_path,
                        pair[0],
                        pair[1],
                    )
                except OfficialTollStationNotFoundError as error:
                    # An OSM feature can be a valid spatial candidate but not
                    # the directional entry station for this journey.  Let
                    # the next route-progress pair prove itself through the
                    # official station check instead of guessing.
                    last_error = error
                    continue
                except OfficialTollError as error:
                    last_error = error
                    break

            self.metrics["crawl_failure"] += 1
            error_code = (
                last_error.code if last_error is not None else "OFFICIAL_REQUEST_FAILED"
            )
            if TOLL_FAILURE_COOLDOWN_S > 0:
                self._failure_cooldown[flight_key] = (
                    time.monotonic() + TOLL_FAILURE_COOLDOWN_S,
                    error_code,
                )
            if stale_lookup is not None:
                self.metrics["stale_cache_used_after_failure"] += 1
                return (
                    stale_lookup,
                    stale_status,
                    stale_fetched_at,
                    last_error.code if last_error is not None else None,
                    "STALE_CACHE",
                    stale_entry,
                    stale_exit,
                )
            return None, "unavailable", None, error_code, "UNAVAILABLE", None, None

    async def _lookup_with_cache(
        self,
        entry_name: str,
        exit_name: str,
        *,
        entry_gate=None,
        exit_gate=None,
        candidate_pairs: list[tuple[object, object]] | None = None,
        route_distance_m: float | None = None,
    ) -> tuple[
        OfficialTollLookup | None,
        str,
        datetime | None,
        str | None,
        str,
        object | None,
        object | None,
    ]:
        pairs = candidate_pairs or [(entry_gate, exit_gate)]
        key = self._pair_flight_key(entry_name, exit_name, pairs)
        async with self._inflight_lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._run_shared_lookup(
                        key,
                        self._lookup_with_cache_uncached(
                            entry_name,
                            exit_name,
                            entry_gate=entry_gate,
                            exit_gate=exit_gate,
                            candidate_pairs=pairs,
                            flight_key=key,
                            route_distance_m=route_distance_m,
                        ),
                    )
                )
                self._inflight[key] = task
                self.metrics["singleflight_owner"] += 1
            else:
                self.metrics["singleflight_waiter"] += 1
        return await asyncio.shield(task)

    @staticmethod
    def _source_failure_code(value: str | None) -> str:
        valid = {item.value for item in TollFailureCode}
        if value in valid:
            return value
        if value in {"PARSER_ERROR", "TOLL_SOURCE_UNAVAILABLE", None}:
            return TollFailureCode.OFFICIAL_REQUEST_FAILED.value
        return TollFailureCode.OFFICIAL_REQUEST_FAILED.value

    @staticmethod
    def _source_failure_stage(code: str) -> TollStage:
        if code == TollFailureCode.OFFICIAL_STATION_NOT_FOUND.value:
            return TollStage.OFFICIAL_STATION_MATCH_OK
        if code in {
            TollFailureCode.OFFICIAL_PARSE_FAILED.value,
            TollFailureCode.PRICE_NOT_FOUND.value,
        }:
            return TollStage.OFFICIAL_RESULT_PARSE_OK
        return TollStage.OFFICIAL_PAGE_REQUEST_OK

    @staticmethod
    def _distance_is_consistent(route_distance_m: float, lookup: OfficialTollLookup) -> bool:
        if lookup.distance_km is None:
            return True
        official_distance_m = lookup.distance_km * 1000.0
        mismatch = abs(route_distance_m - official_distance_m)
        allowed = max(
            TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_M,
            route_distance_m * TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_RATIO,
        )
        return mismatch <= allowed

    @staticmethod
    def _official_station_identity(value: str) -> str:
        query = official_query_name(value)
        canonical = OBSERVED_OFFICIAL_STATION_ALIASES.get(query, query)
        return normalize_toll_name(canonical)

    @classmethod
    def _official_pair_is_consistent(
        cls,
        entry_name: str,
        exit_name: str,
        lookup: OfficialTollLookup,
    ) -> bool:
        requested_entry = cls._official_station_identity(entry_name)
        requested_exit = cls._official_station_identity(exit_name)
        return (
            requested_entry == cls._official_station_identity(lookup.entry_name)
            and requested_exit == cls._official_station_identity(lookup.exit_name)
        )

    @staticmethod
    def _named_entry_exit(analysis) -> tuple[MatchedTollGate, MatchedTollGate] | None:
        """Resolve one journey from logical route-progress gates.

        Spatial candidates are not charges.  Only the first and last distinct,
        named logical gates on the same supported route corridor become the
        official entry/exit pair; all other logical gates remain intermediate
        evidence.  The selection is allowed only after duplicate clusters
        have been collapsed, route positions are monotonic, names are
        distinct, and explicit operator context is not contradictory.
        """

        ordered = sorted(
            analysis.gates,
            key=lambda match: (match.position_along_route_m, match.distance_to_route_m),
        )
        named = [
            match
            for match in ordered
            if match.gate.name and match.gate.name.strip()
        ]
        if len(named) < 2:
            return None
        normalized_names = [normalize_toll_name(match.gate.name) for match in named]
        if any(not value for value in normalized_names):
            return None
        if len(set(normalized_names)) != len(normalized_names):
            # Repeated names in different logical clusters can indicate a
            # loop, parallel carriageway, or an unresolved duplicate.  Do not
            # invent an entry/exit pair from that evidence.
            return None
        if named[0].position_along_route_m >= named[-1].position_along_route_m:
            return None
        explicit_operators = {
            match.gate.operator.strip().casefold()
            for match in named
            if match.gate.operator and match.gate.operator.strip()
        }
        if len(explicit_operators) > 1 and not analysis.toll_road_detected:
            return None
        return named[0], named[-1]

    @staticmethod
    def _entry_exit_candidates(
        analysis, *, max_attempts: int = 8
    ) -> list[tuple[MatchedTollGate, MatchedTollGate]]:
        """Return route-progress pairs for official directional validation.

        The first spatially named gate is not necessarily a usable entry for
        the travel direction: a route can pass an exit-only toll facility or
        a nearby branch before reaching its actual entry station.  Keep the
        exit candidates in reverse route order and advance the entry candidate
        only after the official station/path check rejects the prior pair.
        This is still one official journey lookup, never a sum of candidates.
        """

        ordered = sorted(
            analysis.gates,
            key=lambda match: (match.position_along_route_m, match.distance_to_route_m),
        )
        named = [
            match
            for match in ordered
            if match.gate.name and match.gate.name.strip()
        ]
        if len(named) < 2:
            return []
        normalized_names = [normalize_toll_name(match.gate.name) for match in named]
        if any(not value for value in normalized_names):
            return []
        if len(set(normalized_names)) != len(normalized_names):
            return []
        explicit_operators = {
            match.gate.operator.strip().casefold()
            for match in named
            if match.gate.operator and match.gate.operator.strip()
        }
        if len(explicit_operators) > 1 and not analysis.toll_road_detected:
            return []

        pairs: list[tuple[MatchedTollGate, MatchedTollGate]] = []
        for exit_index in range(len(named) - 1, 0, -1):
            for entry_index in range(exit_index):
                pairs.append((named[entry_index], named[exit_index]))
                if len(pairs) >= max_attempts:
                    return pairs
        return pairs

    @staticmethod
    def _entry_exit_note(analysis, pair: tuple[MatchedTollGate, MatchedTollGate] | None) -> str:
        if pair is None:
            return (
                "entry/exit unresolved: logical route-progression evidence had "
                "fewer than two distinct named gates or conflicting context"
            )
        entry, exit = pair
        intermediate_count = max(0, len(analysis.gates) - 2)
        return (
            "entry/exit selected after route progression, road/operator context, "
            f"and duplicate grouping: {entry.gate.name} -> {exit.gate.name}; "
            f"intermediate logical gates={intermediate_count}"
        )

    @staticmethod
    def _apply_candidate_roles(
        logical_gates: list[MatchedTollGate],
        raw_candidates: list[MatchedTollGate],
        entry: MatchedTollGate,
        exit: MatchedTollGate,
    ) -> tuple[list[MatchedTollGate], list[MatchedTollGate]]:
        def role_for(match: MatchedTollGate) -> str:
            if match.gate.id == entry.gate.id:
                return "entry"
            if match.gate.id == exit.gate.id:
                return "exit"
            return "intermediate"

        logical = [match.model_copy(update={"candidate_role": role_for(match)}) for match in logical_gates]
        entry_group = entry.duplicate_group
        exit_group = exit.duplicate_group
        raw = []
        for match in raw_candidates:
            if match.gate.id == entry.gate.id or (
                entry_group and match.duplicate_group == entry_group
            ):
                role = "entry"
            elif match.gate.id == exit.gate.id or (
                exit_group and match.duplicate_group == exit_group
            ):
                role = "exit"
            else:
                role = "intermediate"
            raw.append(match.model_copy(update={"candidate_role": role}))
        return logical, raw

    @staticmethod
    def _candidate_details(
        candidates: list[MatchedTollGate], *, route_distance_m: float
    ) -> list[dict[str, object]]:
        details: list[dict[str, object]] = []
        for index, candidate in enumerate(candidates, start=1):
            tags = candidate.gate.tags
            details.append(
                {
                    "candidate_number": index,
                    "id": candidate.gate.id,
                    "raw_osm_name": candidate.gate.name,
                    "normalized_name": candidate.gate.normalized_name,
                    "lat": candidate.gate.lat,
                    "lng": candidate.gate.lng,
                    "osm_id": candidate.gate.osm_id,
                    "osm_type": candidate.gate.osm_type,
                    "gate_type": candidate.gate.gate_type,
                    "ref": candidate.gate.ref,
                    "barrier": tags.get("barrier"),
                    "highway": tags.get("highway"),
                    "toll": tags.get("toll"),
                    "operator": candidate.gate.operator,
                    "road_name": candidate.gate.road_name,
                    "route_distance_m": route_distance_m,
                    "distance_to_route_m": candidate.distance_to_route_m,
                    "position_along_route_m": candidate.position_along_route_m,
                    "duplicate_group": candidate.duplicate_group,
                    "candidate_role": candidate.candidate_role,
                }
            )
        return details

    @staticmethod
    def _station_reference(
        lookup: OfficialTollLookup, *, entry: bool, matched_osm_name: str
    ) -> OfficialStationReference | None:
        official_id = lookup.entry_official_id if entry else lookup.exit_official_id
        official_name = lookup.entry_name if entry else lookup.exit_name
        if not official_id:
            return None
        confidence = (
            "verified_exact"
            if matched_osm_name.strip() == official_name.strip()
            else "verified_alias"
        )
        return OfficialStationReference(
            official_id=official_id,
            official_name=official_name,
            confidence=confidence,
            matched_osm_name=matched_osm_name,
        )

    async def calculate(self, request: TollCalculationRequest) -> TollResponse:
        self._validate_route_binding(request)
        route_id = self._route_id(request)
        diagnostics = TollDiagnostics(
            stage=TollStage.ROUTE_OK,
            completed_stages=[TollStage.ROUTE_OK],
        )
        self._store_debug(route_id, diagnostics)

        coordinates = [tuple(point) for point in request.route.geometry.coordinates]
        route_distance_m = request.route.distance_m
        analysis_started = time.perf_counter()
        try:
            analysis = self.index.analyze_route(coordinates)
        except TollIndexUnavailableError as error:
            self.metrics["index_unavailable"] += 1
            raise TollIndexServiceUnavailableError(str(error)) from error
        analysis_ms = round((time.perf_counter() - analysis_started) * 1000, 2)
        if TOLL_DEBUG_MODE:
            diagnostics = diagnostics.model_copy(
                update={
                    "timings_ms": {
                        "tg_analysis_ms": analysis_ms,
                        **getattr(self.index, "last_timings", {}),
                    }
                }
            )

        raw_candidates = list(analysis.raw_candidates or analysis.gates)
        logical_gates = list(analysis.gates)
        if not raw_candidates:
            self.metrics["toll_free_unproven"] += 1
            diagnostics = self._failure(
                diagnostics,
                stage=TollStage.TOLL_CANDIDATES_FOUND,
                code=TollFailureCode.NO_TOLL_CANDIDATES,
            )
            self._store_debug(route_id, diagnostics)
            result = self._partial_result(
                request=request,
                route_id=route_id,
                # Keep the existing public reason for clients while the
                # diagnostics field carries the precise stage code.
                reason="toll_status_not_proven",
                detected_gates=[],
                logical_gates=[],
                diagnostics=diagnostics,
            )
            return TollResponse(status="partial", toll=result)

        diagnostics = self._advance(
            diagnostics,
            TollStage.TOLL_CANDIDATES_FOUND,
            raw_candidates=len(raw_candidates),
        )
        diagnostics = self._advance(
            diagnostics,
            TollStage.TOLLGATE_DEDUP_OK,
            logical_gates=len(logical_gates),
            duplicate_groups=analysis.duplicate_groups,
        )
        diagnostics = diagnostics.model_copy(
            update={
                "candidate_details": self._candidate_details(
                    raw_candidates, route_distance_m=route_distance_m
                )
            }
        )

        # A route can legitimately contain both KEC-managed ways and a nearby
        # private/operator-tagged way (parallel carriageway, connector, or a
        # mixed corridor such as the Seoul-Busan route).  Do not reject that
        # journey before asking the official source: the exact official
        # entry/exit result and distance sanity check are the evidence that
        # the public source covers the whole journey.  A private/unknown-only
        # corridor still remains fail-safe and never becomes a guessed total.
        private_only = analysis.unsupported_private_road and not analysis.supported_operator_evidence
        if private_only or analysis.unknown_toll_operator:
            self.metrics["private_toll_road"] += 1
            diagnostics = self._failure(
                diagnostics,
                stage=TollStage.ENTRY_EXIT_RESOLUTION_OK,
                code=TollFailureCode.PRIVATE_SEGMENT_UNRESOLVED,
            )
            diagnostics = diagnostics.model_copy(
                update={
                    "notes": [
                        *diagnostics.notes,
                        "entry/exit resolution stopped because a private or "
                        "unresolved operator segment remains",
                    ]
                }
            )
            self._store_debug(route_id, diagnostics)
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason=(
                    "unknown_toll_operator"
                    if analysis.unknown_toll_operator and not analysis.unsupported_private_road
                    else TollFailureCode.PRIVATE_SEGMENT_UNRESOLVED.value
                ),
                detected_gates=raw_candidates,
                logical_gates=logical_gates,
                diagnostics=diagnostics,
            )
            return TollResponse(status="partial", toll=result)

        if analysis.unsupported_private_road:
            diagnostics = diagnostics.model_copy(
                update={
                    "notes": [
                        *diagnostics.notes,
                        "mixed operator evidence detected; official full-journey "
                        "result and distance are required before completion",
                    ]
                }
            )

        candidate_pairs = self._entry_exit_candidates(analysis)
        named_pair = candidate_pairs[0] if candidate_pairs else None
        diagnostics = diagnostics.model_copy(
            update={
                "notes": [
                    *diagnostics.notes,
                    self._entry_exit_note(analysis, named_pair),
                    f"official directional pair candidates={len(candidate_pairs)}",
                ]
            }
        )
        if not candidate_pairs or len(logical_gates) < 2:
            self.metrics["gate_match_failure"] += 1
            named_count = sum(
                1
                for match in logical_gates
                if match.gate.name and match.gate.name.strip()
            )
            code = (
                TollFailureCode.AMBIGUOUS_TOLLGATE
                if len(logical_gates) >= 2 and named_count >= 2
                else TollFailureCode.ENTRY_EXIT_UNRESOLVED
            )
            diagnostics = self._failure(
                diagnostics,
                stage=TollStage.ENTRY_EXIT_RESOLUTION_OK,
                code=code,
            )
            self._store_debug(route_id, diagnostics)
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason=code.value,
                detected_gates=raw_candidates,
                logical_gates=logical_gates,
                diagnostics=diagnostics,
            )
            return TollResponse(status="partial", toll=result)

        entry_match, exit_match = candidate_pairs[0]
        logical_with_roles, raw_with_roles = self._apply_candidate_roles(
            logical_gates, raw_candidates, entry_match, exit_match
        )
        diagnostics = diagnostics.model_copy(
            update={
                "candidate_details": self._candidate_details(
                    raw_with_roles, route_distance_m=route_distance_m
                )
            }
        )
        entry_gate = entry_match.gate
        exit_gate = exit_match.gate
        entry_name = official_query_name(entry_gate.name)
        exit_name = official_query_name(exit_gate.name)
        if (
            not entry_name
            or not exit_name
            or normalize_toll_name(entry_name) == normalize_toll_name(exit_name)
        ):
            self.metrics["gate_name_failure"] += 1
            diagnostics = self._failure(
                diagnostics,
                stage=TollStage.ENTRY_EXIT_RESOLUTION_OK,
                code=TollFailureCode.ENTRY_EXIT_UNRESOLVED,
            ).model_copy(
                update={
                    "entry_candidate_id": entry_gate.id,
                    "exit_candidate_id": exit_gate.id,
                }
            )
            self._store_debug(route_id, diagnostics)
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason=TollFailureCode.ENTRY_EXIT_UNRESOLVED.value,
                detected_gates=raw_with_roles,
                logical_gates=logical_with_roles,
                diagnostics=diagnostics,
            )
            return TollResponse(status="partial", toll=result)

        diagnostics = self._advance(diagnostics, TollStage.ENTRY_EXIT_RESOLUTION_OK)
        diagnostics = diagnostics.model_copy(
            update={
                "entry_candidate_id": entry_gate.id,
                "exit_candidate_id": exit_gate.id,
            }
        )

        browser_client = getattr(self.crawler, "browser_client", None)
        http_client = getattr(self.crawler, "http_client", None)
        for source_client in (browser_client, http_client, self.crawler):
            if source_client is not None and hasattr(source_client, "last_request_events"):
                # Do not attach a previous request's network trace to a cache hit.
                source_client.last_request_events = []
            if source_client is not None and hasattr(source_client, "last_timings"):
                # Timing metadata is request-scoped.  Keeping an earlier HTTP
                # sample here made a later CACHE response look as if it had
                # paid the old network latency.
                source_client.last_timings = {}
            if source_client is not None and hasattr(source_client, "last_result_route_labels"):
                source_client.last_result_route_labels = []
        lookup_entry_name = entry_gate.name or entry_name
        lookup_exit_name = exit_gate.name or exit_name
        lookup_started = time.perf_counter()
        (
            lookup,
            source_status,
            fetched_at,
            source_error,
            lookup_origin,
            resolved_entry_match,
            resolved_exit_match,
        ) = await self._lookup_with_cache(
            lookup_entry_name,
            lookup_exit_name,
            entry_gate=entry_gate,
            exit_gate=exit_gate,
            candidate_pairs=candidate_pairs,
            route_distance_m=route_distance_m,
        )
        lookup_ms = round((time.perf_counter() - lookup_started) * 1000, 2)
        request_events = (
            []
            if lookup_origin in {"CACHE", "STALE_CACHE"}
            else list(getattr(self.crawler, "last_request_events", []))
        )
        if request_events:
            diagnostics = diagnostics.model_copy(update={"request_events": request_events[-300:]})
        station_count = getattr(browser_client, "last_station_directory_count", 0)
        if station_count:
            diagnostics = diagnostics.model_copy(
                update={"official_station_count": int(station_count)}
            )
        if TOLL_DEBUG_MODE:
            source_timings = dict(getattr(self.crawler, "last_timings", {}))
            diagnostics = diagnostics.model_copy(
                update={
                    "timings_ms": {
                        **diagnostics.timings_ms,
                        "toll_lookup_ms": lookup_ms,
                        **source_timings,
                    }
                }
            )
        self.last_lookup_diagnostics = {
            "source_path": lookup_origin,
            "cache_status": source_status,
            "duration_ms": lookup_ms,
            "source_timings_ms": dict(getattr(self.crawler, "last_timings", {})),
        }
        if lookup is None:
            self.metrics["official_lookup_failure"] += 1
            code = self._source_failure_code(source_error)
            diagnostics = self._failure(
                diagnostics,
                stage=self._source_failure_stage(code),
                code=TollFailureCode(code),
            ).model_copy(
                update={"official_lookup": "failed"}
            )
            self._store_debug(route_id, diagnostics)
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason=code,
                detected_gates=raw_with_roles,
                logical_gates=logical_with_roles,
                diagnostics=diagnostics,
            )
            return TollResponse(status="partial", toll=result)

        if isinstance(resolved_entry_match, MatchedTollGate) and isinstance(
            resolved_exit_match, MatchedTollGate
        ):
            entry_match = resolved_entry_match
            exit_match = resolved_exit_match
            logical_with_roles, raw_with_roles = self._apply_candidate_roles(
                logical_gates, raw_candidates, entry_match, exit_match
            )
            diagnostics = diagnostics.model_copy(
                update={
                    "candidate_details": self._candidate_details(
                        raw_with_roles, route_distance_m=route_distance_m
                    ),
                    "entry_candidate_id": entry_match.gate.id,
                    "exit_candidate_id": exit_match.gate.id,
                    "notes": [
                        *diagnostics.notes,
                        f"official path validation selected {entry_match.gate.name} -> "
                        f"{exit_match.gate.name}",
                    ],
                }
            )
            entry_gate = entry_match.gate
            exit_gate = exit_match.gate
            entry_name = official_query_name(entry_gate.name)
            exit_name = official_query_name(exit_gate.name)

        entry_ref = self._station_reference(
            lookup, entry=True, matched_osm_name=entry_gate.name or ""
        )
        exit_ref = self._station_reference(
            lookup, entry=False, matched_osm_name=exit_gate.name or ""
        )
        diagnostics = diagnostics.model_copy(
            update={
                "official_lookup": (
                    "cache"
                    if lookup_origin in {"CACHE", "STALE_CACHE"}
                    else "success"
                ),
                "source_path": lookup_origin
                if lookup_origin in {"CACHE", "HTTP", "PLAYWRIGHT", "STALE_CACHE"}
                else "UNAVAILABLE",
                "official_entry": entry_ref,
                "official_exit": exit_ref,
            }
        )
        # BrowserTollClient only returns a parsed lookup after the station
        # directory and canonical IDs have both succeeded.  HTTP test/fallback
        # adapters may not expose IDs, so the stage is still recorded from the
        # verified route names in that compatibility path.
        diagnostics = self._advance(diagnostics, TollStage.OFFICIAL_STATION_MATCH_OK)
        diagnostics = self._advance(diagnostics, TollStage.OFFICIAL_PAGE_REQUEST_OK)
        diagnostics = self._advance(diagnostics, TollStage.OFFICIAL_RESULT_PARSE_OK)

        expected_official_entry = (
            entry_ref.official_name if entry_ref is not None else entry_name
        )
        expected_official_exit = (
            exit_ref.official_name if exit_ref is not None else exit_name
        )
        if not self._official_pair_is_consistent(
            expected_official_entry,
            expected_official_exit,
            lookup,
        ):
            self.metrics["official_pair_mismatch"] += 1
            diagnostics = self._failure(
                diagnostics,
                stage=TollStage.OFFICIAL_RESULT_PARSE_OK,
                code=TollFailureCode.TOLL_ROUTE_MISMATCH,
            )
            self._store_debug(route_id, diagnostics)
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason=TollFailureCode.TOLL_ROUTE_MISMATCH.value,
                detected_gates=raw_with_roles,
                logical_gates=logical_with_roles,
                diagnostics=diagnostics,
            )
            return TollResponse(status="partial", toll=result)

        if not self._distance_is_consistent(request.route.distance_m, lookup):
            self.metrics["official_distance_mismatch"] += 1
            diagnostics = self._failure(
                diagnostics,
                stage=TollStage.OFFICIAL_RESULT_PARSE_OK,
                code=TollFailureCode.TOLL_ROUTE_MISMATCH,
            )
            self._store_debug(route_id, diagnostics)
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason=TollFailureCode.TOLL_ROUTE_MISMATCH.value,
                detected_gates=raw_with_roles,
                logical_gates=logical_with_roles,
                diagnostics=diagnostics,
            )
            return TollResponse(status="partial", toll=result)

        try:
            amount = lookup.prices[request.vehicle_class]
        except KeyError:
            self.metrics["vehicle_price_missing"] += 1
            diagnostics = self._failure(
                diagnostics,
                stage=TollStage.VEHICLE_PRICE_FOUND,
                code=TollFailureCode.PRICE_NOT_FOUND,
            )
            self._store_debug(route_id, diagnostics)
            result = self._partial_result(
                request=request,
                route_id=route_id,
                reason=TollFailureCode.PRICE_NOT_FOUND.value,
                detected_gates=raw_with_roles,
                logical_gates=logical_with_roles,
                diagnostics=diagnostics,
            )
            return TollResponse(status="partial", toll=result)

        diagnostics = self._advance(diagnostics, TollStage.VEHICLE_PRICE_FOUND)
        diagnostics = self._advance(diagnostics, TollStage.TOLL_COMPLETE)
        self._store_debug(route_id, diagnostics)
        journey = TollJourney(
            entry=entry_gate,
            exit=exit_gate,
            operator=self._operator(analysis),
            route_distance_m=request.route.distance_m,
            toll_krw=amount,
            source=lookup.source,
            confidence="verified",
            source_status=source_status,
            fetched_at=fetched_at,
            official_entry_id=lookup.entry_official_id,
            official_exit_id=lookup.exit_official_id,
        )
        result = TollResult(
            status="ok",
            complete=True,
            vehicle_class=request.vehicle_class,
            total_toll_krw=amount,
            known_toll_krw=amount,
            journeys=[journey],
            detected_toll_gates=raw_with_roles,
            logical_toll_gates=logical_with_roles,
            unknown_segments=0,
            reason="official_web_verified" if source_status == "fresh" else "official_web_stale_cache",
            source_status=source_status,
            fetched_at=fetched_at,
            route_id=route_id,
            diagnostics=diagnostics,
        )
        return TollResponse(status="ok", toll=result)
