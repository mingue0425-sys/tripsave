"""SQLite cache isolated from accommodation, fuel, toll, and common tables."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from backend.places.models import PlaceRecord


LOGGER = logging.getLogger(__name__)
DEFAULT_CACHE_FILE = Path(
    os.getenv("KTO_PLACES_CACHE_FILE", str(Path(__file__).resolve().parents[2] / "data" / "places_cache.sqlite3"))
)


@dataclass(frozen=True)
class PlaceCacheEntry:
    results: list[PlaceRecord]
    fetched_at: datetime


def make_cache_key(
    *,
    source: str,
    category: str,
    label: str,
    lat: float,
    lng: float,
    radius_km: float,
) -> str:
    payload = {
        "source": source,
        "category": category,
        "label": label.strip().casefold(),
        "lat": round(float(lat), 5),
        "lng": round(float(lng), 5),
        "radius_km": round(float(radius_km), 2),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PlaceCache:
    """Small best-effort cache for slow-changing place metadata."""

    def __init__(self, path: Path | str = DEFAULT_CACHE_FILE) -> None:
        self.path = Path(path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS place_search_cache (
                        cache_key TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        category TEXT NOT NULL,
                        results_json TEXT NOT NULL,
                        fetched_at TEXT NOT NULL
                    )
                    """
                )
        except (OSError, sqlite3.Error) as error:
            LOGGER.warning("Place cache unavailable: %s", error)

    def put(
        self,
        key: str,
        *,
        source: str,
        category: str,
        results: Iterable[PlaceRecord],
        fetched_at: datetime,
    ) -> None:
        serialized = json.dumps(
            [record.model_dump(mode="json") for record in results],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO place_search_cache
                        (cache_key, source, category, results_json, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        source=excluded.source,
                        category=excluded.category,
                        results_json=excluded.results_json,
                        fetched_at=excluded.fetched_at
                    """,
                    (key, source, category, serialized, _as_utc(fetched_at).isoformat()),
                )
        except (OSError, sqlite3.Error) as error:
            LOGGER.warning("Could not write place cache: %s", error)

    def get(self, key: str, *, max_age_seconds: float | None = None) -> PlaceCacheEntry | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT results_json, fetched_at FROM place_search_cache WHERE cache_key = ?",
                    (key,),
                ).fetchone()
        except (OSError, sqlite3.Error) as error:
            LOGGER.warning("Could not read place cache: %s", error)
            return None
        if row is None:
            return None
        try:
            fetched_at = _parse_datetime(row["fetched_at"])
            if max_age_seconds is not None:
                age = (_as_utc(datetime.now(timezone.utc)) - fetched_at).total_seconds()
                if age < 0 or age > max_age_seconds:
                    return None
            raw_results = json.loads(row["results_json"])
            results = [PlaceRecord.model_validate(item) for item in raw_results]
            return PlaceCacheEntry(results=results, fetched_at=fetched_at)
        except (TypeError, ValueError, json.JSONDecodeError, KeyError) as error:
            LOGGER.warning("Ignoring invalid place cache entry: %s", error)
            return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _as_utc(parsed)


__all__ = ["DEFAULT_CACHE_FILE", "PlaceCache", "PlaceCacheEntry", "make_cache_key"]
