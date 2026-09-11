"""SQLite schema and compact toll-way geometry serialization."""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterable


SCHEMA_VERSION = "1"
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS toll_gates (
    id TEXT PRIMARY KEY,
    osm_type TEXT NOT NULL,
    osm_id INTEGER NOT NULL,
    name TEXT,
    normalized_name TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    road_name TEXT,
    operator TEXT,
    ref TEXT,
    gate_type TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_toll_gates_normalized_name
    ON toll_gates(normalized_name);
CREATE INDEX IF NOT EXISTS idx_toll_gates_osm_id
    ON toll_gates(osm_type, osm_id);

CREATE TABLE IF NOT EXISTS official_stations (
    official_id TEXT PRIMARY KEY,
    official_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    road_code TEXT,
    road_name TEXT,
    verified_at TEXT NOT NULL,
    source_url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_official_stations_normalized_name
    ON official_stations(normalized_name);

CREATE TABLE IF NOT EXISTS toll_road_ways (
    id INTEGER PRIMARY KEY,
    osm_id INTEGER NOT NULL UNIQUE,
    name TEXT,
    ref TEXT,
    operator TEXT,
    min_lng REAL NOT NULL,
    max_lng REAL NOT NULL,
    min_lat REAL NOT NULL,
    max_lat REAL NOT NULL,
    point_count INTEGER NOT NULL,
    geometry BLOB NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_toll_road_bbox_lng
    ON toll_road_ways(min_lng, max_lng);
CREATE INDEX IF NOT EXISTS idx_toll_road_bbox_lat
    ON toll_road_ways(min_lat, max_lat);

CREATE VIRTUAL TABLE IF NOT EXISTS toll_road_rtree USING rtree(
    id,
    min_lng, max_lng,
    min_lat, max_lat
);

CREATE TABLE IF NOT EXISTS toll_rates_cache (
    cache_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    entry_name TEXT NOT NULL,
    entry_normalized TEXT NOT NULL,
    entry_official_id TEXT,
    exit_name TEXT NOT NULL,
    exit_normalized TEXT NOT NULL,
    exit_official_id TEXT,
    prices_json TEXT NOT NULL,
    distance_km REAL,
    route_description TEXT,
    source_url TEXT NOT NULL,
    raw_evidence_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    parser_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_toll_rates_expiry
    ON toll_rates_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_toll_rates_entry_exit
    ON toll_rates_cache(entry_normalized, exit_normalized, fetched_at);

CREATE TABLE IF NOT EXISTS fuel_prices_cache (
    cache_key TEXT PRIMARY KEY,
    fuel_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    region TEXT,
    price_krw_per_l REAL NOT NULL,
    unit TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TEXT,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    raw_evidence_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fuel_prices_expiry
    ON fuel_prices_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_fuel_prices_identity
    ON fuel_prices_cache(fuel_type, scope, region, fetched_at);
"""


def connect_database(path: str, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    # The generated OSM index predates official station IDs.  Keep its schema
    # version stable so an existing PBF build remains readable, while adding
    # the nullable cache columns in place for existing databases.
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(toll_rates_cache)").fetchall()
    }
    for column in ("entry_official_id", "exit_official_id"):
        if column not in columns:
            connection.execute(f"ALTER TABLE toll_rates_cache ADD COLUMN {column} TEXT")
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        ("schema_version", SCHEMA_VERSION),
    )
    connection.commit()


def encode_geometry(coordinates: Iterable[tuple[float, float]]) -> bytes:
    flattened: list[float] = []
    for longitude, latitude in coordinates:
        flattened.extend((float(longitude), float(latitude)))
    if len(flattened) < 4:
        raise ValueError("a toll road geometry needs at least two coordinates")
    return struct.pack(f"<{len(flattened)}d", *flattened)


def decode_geometry(value: bytes, point_count: int) -> list[tuple[float, float]]:
    if not isinstance(value, bytes) or point_count < 2 or len(value) != point_count * 16:
        raise ValueError("invalid serialized toll road geometry")
    values = struct.unpack(f"<{point_count * 2}d", value)
    return list(zip(values[0::2], values[1::2]))
