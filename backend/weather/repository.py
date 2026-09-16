"""Small independent SQLite cache for daily weather responses."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class WeatherCacheEntry:
    payload: list[dict[str, object]]
    fetched_at: datetime
    fresh: bool


class WeatherCache:
    SCHEMA_VERSION = "weather-v1"

    def __init__(self, database_path: str | Path, *, ttl_s: float = 10_800.0, stale_max_age_s: float = 172_800.0) -> None:
        self.database_path = Path(database_path)
        self.ttl_s = max(1.0, float(ttl_s))
        self.stale_max_age_s = max(self.ttl_s, float(stale_max_age_s))
        self._closed = False
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS weather_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )

    def get(self, cache_key: str, *, now: datetime | None = None) -> WeatherCacheEntry | None:
        reference = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, fetched_at, expires_at FROM weather_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        expires_at = datetime.fromisoformat(row["expires_at"])
        if reference > expires_at + timedelta(seconds=self.stale_max_age_s):
            return None
        return WeatherCacheEntry(
            payload=json.loads(row["payload_json"]),
            fetched_at=fetched_at,
            fresh=reference <= expires_at,
        )

    def put(
        self,
        cache_key: str,
        *,
        provider: str,
        payload: list[dict[str, object]],
        fetched_at: datetime,
    ) -> None:
        expires_at = fetched_at + timedelta(seconds=self.ttl_s)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO weather_cache(cache_key, provider, payload_json, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    provider=excluded.provider,
                    payload_json=excluded.payload_json,
                    fetched_at=excluded.fetched_at,
                    expires_at=excluded.expires_at
                """,
                (
                    cache_key,
                    provider,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True),
                    fetched_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )

    def close(self) -> None:
        # Every cache operation owns and closes its SQLite connection.
        pass
