"""Read-only access to the generated OSM toll index."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from pathlib import Path

from backend.tolls.matcher import (
    Coordinate,
    coordinate_distance_meters,
    match_toll_gates_detailed,
    nearest_point_on_route,
)
from backend.tolls.models import (
    MatchedTollRoad,
    TollAnalysis,
    TollGate,
)
from backend.tolls.names import normalize_toll_name
from backend.tolls.schema import connect_database, decode_geometry
from config import (
    TOLL_GATE_DEDUP_THRESHOLD_M,
    TOLL_GATE_MATCH_THRESHOLD_M,
    TOLL_ROAD_MATCH_THRESHOLD_M,
)


LOGGER = logging.getLogger(__name__)


class TollIndexUnavailableError(RuntimeError):
    """The generated local OSM toll index is missing or malformed."""


def _is_supported_operator(operator: str | None) -> bool:
    if not operator:
        return False
    normalized = normalize_toll_name(operator)
    return "한국도로공사" in normalized or "koreaexpresswaycorporation" in normalized


class TollIndex:
    """Lazy, read-only toll index with route corridor matching."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        # FastAPI's TestClient and production workers can invoke one service
        # instance from more than one thread.  Keep one read-only connection
        # per thread rather than reusing a sqlite connection created elsewhere.
        self._connections: dict[int, sqlite3.Connection] = {}
        self._connection_lock = threading.RLock()
        self._gates: list[TollGate] | None = None

    @property
    def ready(self) -> bool:
        if not self.database_path.is_file() or self.database_path.stat().st_size == 0:
            return False
        try:
            connection = self._get_connection()
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            return row is not None and row[0] == "1"
        except (OSError, sqlite3.Error, TollIndexUnavailableError):
            return False

    def _get_connection(self) -> sqlite3.Connection:
        thread_id = threading.get_ident()
        with self._connection_lock:
            connection = self._connections.get(thread_id)
            if connection is not None:
                return connection
            try:
                connection = connect_database(
                    str(self.database_path),
                    read_only=True,
                    check_same_thread=False,
                )
                connection.execute("SELECT 1 FROM toll_gates LIMIT 1").fetchone()
            except (OSError, sqlite3.Error) as error:
                if connection is not None:
                    connection.close()
                self._connections.pop(thread_id, None)
                raise TollIndexUnavailableError(
                    f"Toll index is unavailable: {self.database_path}"
                ) from error
            self._connections[thread_id] = connection
            return connection

    def close(self) -> None:
        with self._connection_lock:
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()
            self._gates = None

    def stats(self) -> dict[str, object]:
        connection = self._get_connection()
        rows = connection.execute(
            "SELECT key, value FROM metadata WHERE key IN "
            "('pbf_file', 'pbf_size_bytes', 'built_at', 'counts', "
            "'toll_yes_way_operators')"
        ).fetchall()
        result: dict[str, object] = {}
        for row in rows:
            value: object = row[1]
            if row[0] in {"counts", "toll_yes_way_operators"}:
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            result[row[0]] = value
        result["ready"] = True
        return result

    def gates(self) -> list[TollGate]:
        if self._gates is not None:
            return self._gates
        connection = self._get_connection()
        rows = connection.execute(
            """
            SELECT id, osm_type, osm_id, name, normalized_name, lat, lng,
                   road_name, operator, ref, gate_type, tags_json, source
            FROM toll_gates
            ORDER BY osm_type, osm_id
            """
        ).fetchall()
        gates: list[TollGate] = []
        for row in rows:
            try:
                value = dict(row)
                try:
                    value["tags"] = json.loads(value.pop("tags_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    value["tags"] = {}
                    value.pop("tags_json", None)
                gates.append(TollGate.model_validate(value))
            except (TypeError, ValueError):
                continue
        self._gates = gates
        return gates

    def _nearby_toll_roads(
        self,
        route_coordinates: list[Coordinate],
        threshold_m: float,
    ) -> list[MatchedTollRoad]:
        connection = self._get_connection()
        cumulative_positions = [0.0]
        for first, second in zip(route_coordinates, route_coordinates[1:]):
            cumulative_positions.append(
                cumulative_positions[-1] + coordinate_distance_meters(first, second)
            )

        matched: dict[int, MatchedTollRoad] = {}
        latitude_scale = 111_320.0
        for index, point in enumerate(route_coordinates):
            longitude, latitude = point
            latitude_delta = threshold_m / latitude_scale
            longitude_delta = threshold_m / max(
                1.0, latitude_scale * math.cos(math.radians(latitude))
            )
            rows = connection.execute(
                """
                SELECT w.id, w.osm_id, w.name, w.ref, w.operator,
                       w.point_count, w.geometry
                FROM toll_road_rtree AS r
                JOIN toll_road_ways AS w ON w.id = r.id
                WHERE r.min_lng <= ? AND r.max_lng >= ?
                  AND r.min_lat <= ? AND r.max_lat >= ?
                """,
                (
                    longitude + longitude_delta,
                    longitude - longitude_delta,
                    latitude + latitude_delta,
                    latitude - latitude_delta,
                ),
            ).fetchall()
            for row in rows:
                database_id = int(row[0])
                if database_id in matched:
                    continue
                try:
                    geometry = decode_geometry(row[6], int(row[5]))
                    distance_m, _ = nearest_point_on_route(point, geometry)
                except (TypeError, ValueError):
                    continue
                if distance_m > threshold_m:
                    continue
                matched[database_id] = MatchedTollRoad(
                    osm_id=int(row[1]),
                    name=row[2],
                    ref=row[3],
                    operator=row[4],
                    distance_to_route_m=distance_m,
                    position_along_route_m=cumulative_positions[index],
                    confidence=(
                        "high"
                        if distance_m <= 50
                        else "medium"
                        if distance_m <= threshold_m * 0.6
                        else "low"
                    ),
                )
        return sorted(
            matched.values(),
            key=lambda road: (road.position_along_route_m, road.distance_to_route_m),
        )

    def analyze_route(
        self,
        route_coordinates: list[Coordinate],
        *,
        gate_threshold_m: float = TOLL_GATE_MATCH_THRESHOLD_M,
        gate_deduplication_threshold_m: float = TOLL_GATE_DEDUP_THRESHOLD_M,
        road_threshold_m: float = TOLL_ROAD_MATCH_THRESHOLD_M,
    ) -> TollAnalysis:
        if not self.ready:
            raise TollIndexUnavailableError(
                "Local OSM toll index is not ready. Run the toll index setup procedure."
            )
        gates, raw_candidates = match_toll_gates_detailed(
            route_coordinates,
            self.gates(),
            threshold_m=gate_threshold_m,
            deduplication_threshold_m=gate_deduplication_threshold_m,
        )
        route_length_m = sum(
            coordinate_distance_meters(first, second)
            for first, second in zip(route_coordinates, route_coordinates[1:])
        )
        for candidate_number, candidate in enumerate(raw_candidates, start=1):
            tags = candidate.gate.tags
            LOGGER.info(
                "Toll Candidate #%d raw_osm_name=%r normalized_name=%r "
                "lat=%.7f lng=%.7f id=%s osm_id=%d osm_type=%s gate_type=%s ref=%r "
                "barrier=%r highway=%r toll=%r operator=%r road_name=%r "
                "route_distance_m=%.1f distance_to_route_m=%.1f "
                "position_along_route_m=%.1f duplicate_group=%s "
                "candidate_role=%s",
                candidate_number,
                candidate.gate.name,
                candidate.gate.normalized_name,
                candidate.gate.lat,
                candidate.gate.lng,
                candidate.gate.id,
                candidate.gate.osm_id,
                candidate.gate.osm_type,
                candidate.gate.gate_type,
                candidate.gate.ref,
                tags.get("barrier"),
                tags.get("highway"),
                tags.get("toll"),
                candidate.gate.operator,
                candidate.gate.road_name,
                route_length_m,
                candidate.distance_to_route_m,
                candidate.position_along_route_m,
                candidate.duplicate_group,
                candidate.candidate_role,
            )
        LOGGER.info(
            "Toll gate grouping raw_candidates=%d logical_tollgates=%d duplicate_groups=%d",
            len(raw_candidates),
            len(gates),
            sum(1 for gate in gates if gate.duplicate_count > 1),
        )
        roads = self._nearby_toll_roads(route_coordinates, road_threshold_m)
        supported_operator_evidence = any(
            road.operator and _is_supported_operator(road.operator) for road in roads
        ) or any(
            gate.gate.operator and _is_supported_operator(gate.gate.operator) for gate in gates
        )
        unsupported_private = any(
            road.operator and not _is_supported_operator(road.operator) for road in roads
        ) or any(
            gate.gate.operator and not _is_supported_operator(gate.gate.operator)
            for gate in gates
        )
        # A missing operator tag on one way is common in OSM.  It is only a
        # blocking unknown-operator condition when the route has no positive
        # Korea Expressway operator evidence at all; otherwise the station
        # and road context still have an auditable supported source.
        operator_values = [road.operator for road in roads]
        operator_values.extend(gate.gate.operator for gate in gates)
        unknown_operator = bool(operator_values) and not supported_operator_evidence and any(
            not operator for operator in operator_values
        )
        return TollAnalysis(
            toll_road_detected=bool(roads),
            toll_roads=roads,
            gates=gates,
            raw_candidates=raw_candidates,
            duplicate_groups=sum(1 for gate in gates if gate.duplicate_count > 1),
            supported_operator_evidence=supported_operator_evidence,
            unsupported_private_road=unsupported_private,
            unknown_toll_operator=unknown_operator,
        )
