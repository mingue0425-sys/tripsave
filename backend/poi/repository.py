"""SQLite-backed local POI index with explicit unavailable semantics."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from backend.geo import haversine_distance_meters

from .models import PoiCategory, PoiRecord, PoiSearchRequest


class PoiIndexUnavailableError(RuntimeError):
    """The local POI index is missing, corrupt, or not ready."""


def _route_distance_meters(point: tuple[float, float], route: list[list[float]]) -> float:
    """Return point-to-route distance using the existing toll geometry helper."""

    from backend.tolls.matcher import nearest_point_on_route

    distance, _position = nearest_point_on_route(point, route)
    return float(distance)


def _distance_to_location(lat: float, lng: float, request: PoiSearchRequest) -> float:
    from backend.models import Location

    return haversine_distance_meters(
        Location(lat=lat, lng=lng),
        request.destination,
    )


def _bbox(lat: float, lng: float, radius_m: float) -> tuple[float, float, float, float]:
    lat_delta = radius_m / 111_320.0
    cos_lat = max(0.1, abs(math.cos(math.radians(lat))))
    lng_delta = radius_m / (111_320.0 * cos_lat)
    return lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta


class PoiRepository:
    """A separate SQLite file so POI reads never share the toll cache DB."""

    SCHEMA_VERSION = "poi-v1"

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._closed = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def ensure_schema(self) -> None:
        """Create an index explicitly (startup never creates a fake empty index)."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS poi_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS poi_records (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_id TEXT,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    lat REAL NOT NULL,
                    lng REAL NOT NULL,
                    address TEXT,
                    metadata_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_poi_records_category
                    ON poi_records(category);
                CREATE INDEX IF NOT EXISTS idx_poi_records_lat_lng
                    ON poi_records(lat, lng);
                """
            )
            connection.execute(
                """
                INSERT INTO poi_metadata(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (self.SCHEMA_VERSION,),
            )

    def _schema_ready(self) -> bool:
        if not self.database_path.is_file():
            return False
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                names = {str(row[0]) for row in rows}
                if not {"poi_metadata", "poi_records"}.issubset(names):
                    return False
                value = connection.execute(
                    "SELECT value FROM poi_metadata WHERE key='schema_version'"
                ).fetchone()
                return bool(value and value[0] == self.SCHEMA_VERSION)
        except sqlite3.Error:
            return False

    @property
    def ready(self) -> bool:
        return self._schema_ready()

    def status(self) -> dict[str, object]:
        if not self.ready:
            return {"status": "unavailable", "source": "local_osm", "ready": False}
        try:
            with self._connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM poi_records").fetchone()[0]
        except sqlite3.Error as error:
            return {"status": "unavailable", "source": "local_osm", "ready": False, "reason": str(error)}
        return {
            "status": "ready",
            "source": "local_osm",
            "ready": True,
            "record_count": int(count),
        }

    def replace_records(self, records: Iterable[PoiRecord]) -> int:
        self.ensure_schema()
        materialized = list(records)
        with self._connect() as connection:
            connection.execute("DELETE FROM poi_records")
            connection.executemany(
                """
                INSERT INTO poi_records(
                    id, source, source_id, name, category, lat, lng, address,
                    metadata_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.id,
                        record.source,
                        record.source_id,
                        record.name,
                        record.category.value,
                        record.lat,
                        record.lng,
                        record.address,
                        json.dumps(record.metadata, ensure_ascii=False, allow_nan=False, sort_keys=True),
                        record.fetched_at.isoformat(),
                    )
                    for record in materialized
                ],
            )
        return len(materialized)

    def _rows_in_bbox(
        self,
        connection: sqlite3.Connection,
        categories: list[PoiCategory],
        bounds: tuple[float, float, float, float],
    ) -> list[sqlite3.Row]:
        min_lat, max_lat, min_lng, max_lng = bounds
        placeholders = ",".join("?" for _ in categories)
        return connection.execute(
            f"""
            SELECT id, source, source_id, name, category, lat, lng, address,
                   metadata_json, fetched_at
            FROM poi_records
            WHERE category IN ({placeholders})
              AND lat BETWEEN ? AND ?
              AND lng BETWEEN ? AND ?
            """,
            [*(category.value for category in categories), min_lat, max_lat, min_lng, max_lng],
        ).fetchall()

    def search(self, request: PoiSearchRequest) -> list[PoiRecord]:
        if not self.ready:
            raise PoiIndexUnavailableError("local POI index is unavailable")
        route_coordinates = (
            request.route.geometry.coordinates if request.route is not None else None
        )
        destination_bounds = _bbox(
            request.destination.lat,
            request.destination.lng,
            request.destination_radius_m,
        )
        bounds = destination_bounds
        if route_coordinates:
            route_lats = [coordinate[1] for coordinate in route_coordinates]
            route_lngs = [coordinate[0] for coordinate in route_coordinates]
            route_bounds_raw = _bbox(
                sum(route_lats) / len(route_lats),
                sum(route_lngs) / len(route_lngs),
                request.route_corridor_m,
            )
            route_bounds = (
                min(min(route_lats), route_bounds_raw[0]),
                max(max(route_lats), route_bounds_raw[1]),
                min(min(route_lngs), route_bounds_raw[2]),
                max(max(route_lngs), route_bounds_raw[3]),
            )
            bounds = (
                min(bounds[0], route_bounds[0]),
                max(bounds[1], route_bounds[1]),
                min(bounds[2], route_bounds[2]),
                max(bounds[3], route_bounds[3]),
            )

        selected: dict[PoiCategory, list[PoiRecord]] = {
            category: [] for category in request.categories
        }
        with self._connect() as connection:
            rows = self._rows_in_bbox(connection, request.categories, bounds)
        for row in rows:
            destination_distance = _distance_to_location(row["lat"], row["lng"], request)
            route_distance = (
                _route_distance_meters((row["lng"], row["lat"]), route_coordinates)
                if route_coordinates
                else None
            )
            in_destination = destination_distance <= request.destination_radius_m
            in_corridor = route_distance is not None and route_distance <= request.route_corridor_m
            if not in_destination and not in_corridor:
                continue
            record = PoiRecord(
                id=row["id"],
                source=row["source"],
                source_id=row["source_id"],
                name=row["name"],
                category=row["category"],
                lat=row["lat"],
                lng=row["lng"],
                address=row["address"],
                distance_to_destination_m=destination_distance,
                distance_to_route_m=route_distance,
                metadata=json.loads(row["metadata_json"]),
                fetched_at=datetime.fromisoformat(row["fetched_at"]),
            )
            selected[record.category].append(record)

        result: list[PoiRecord] = []
        for category in request.categories:
            result.extend(
                sorted(
                    selected[category],
                    key=lambda item: (
                        min(
                            value
                            for value in (
                                item.distance_to_destination_m,
                                item.distance_to_route_m,
                            )
                            if value is not None
                        ),
                        item.name.casefold(),
                        item.id,
                    ),
                )[: request.limit_per_category]
            )
        return result

    def close(self) -> None:
        # Connections are scoped to each operation and already close through
        # context managers; there is no long-lived handle to leak or tear down.
        pass
