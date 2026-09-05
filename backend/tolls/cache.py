"""SQLite cache for parsed official toll-page results."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.tolls.names import normalize_toll_name
from backend.tolls.official import OfficialTollLookup, PARSER_VERSION
from backend.tolls.schema import connect_database, initialize_schema


class TollCacheError(RuntimeError):
    """The local toll cache could not be read or written."""


@dataclass(frozen=True)
class TollCacheEntry:
    lookup: OfficialTollLookup
    fresh: bool


def _cache_key(entry_name: str, exit_name: str) -> tuple[str, str, str]:
    entry_normalized = normalize_toll_name(entry_name)
    exit_normalized = normalize_toll_name(exit_name)
    if not entry_normalized or not exit_normalized:
        raise TollCacheError("Toll cache keys require both normalized gate names.")
    raw_key = f"official_web|{entry_normalized}|{exit_normalized}"
    return (
        hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
        entry_normalized,
        exit_normalized,
    )


class TollRateCache:
    """Small synchronous cache; DB operations are short and parameterized."""

    def __init__(self, database_path: Path, *, ttl_days: int) -> None:
        self.database_path = Path(database_path)
        self.ttl = timedelta(days=max(1, ttl_days))

    def _connection(self) -> sqlite3.Connection:
        try:
            connection = connect_database(str(self.database_path))
            initialize_schema(connection)
            return connection
        except (OSError, sqlite3.Error) as error:
            raise TollCacheError("The local toll cache database is unavailable.") from error

    def get(
        self,
        entry_name: str,
        exit_name: str,
        *,
        allow_stale: bool = False,
    ) -> TollCacheEntry | None:
        cache_key, entry_normalized, exit_normalized = _cache_key(entry_name, exit_name)
        connection = self._connection()
        try:
            row = connection.execute(
                """
                SELECT entry_name, exit_name, prices_json, distance_km,
                       route_description, source_url, raw_evidence_hash,
                       fetched_at, expires_at, parser_version
                FROM toll_rates_cache
                WHERE cache_key = ? AND entry_normalized = ? AND exit_normalized = ?
                """,
                (cache_key, entry_normalized, exit_normalized),
            ).fetchone()
        except sqlite3.Error as error:
            raise TollCacheError("The local toll cache could not be queried.") from error
        finally:
            connection.close()
        if row is None:
            return None
        try:
            fetched_at = datetime.fromisoformat(row[7])
            expires_at = datetime.fromisoformat(row[8])
            prices = {
                key: int(value)
                for key, value in json.loads(row[2]).items()
            }
            lookup = OfficialTollLookup(
                entry_name=row[0],
                exit_name=row[1],
                route_label=row[0] + "~" + row[1],
                route_stops=[],
                distance_km=row[3],
                prices=prices,
                source_url=row[5],
                fetched_at=fetched_at,
                raw_evidence_hash=row[6],
                parser_version=row[9] or PARSER_VERSION,
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise TollCacheError("The local toll cache contains malformed data.") from error
        fresh = expires_at > datetime.now(timezone.utc)
        if not fresh and not allow_stale:
            return None
        return TollCacheEntry(lookup=lookup, fresh=fresh)

    def put(self, entry_name: str, exit_name: str, lookup: OfficialTollLookup) -> None:
        cache_key, entry_normalized, exit_normalized = _cache_key(entry_name, exit_name)
        now = datetime.now(timezone.utc)
        expires_at = now + self.ttl
        prices = {key.value: value for key, value in lookup.prices.items()}
        connection = self._connection()
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO toll_rates_cache(
                    cache_key, source, entry_name, entry_normalized,
                    exit_name, exit_normalized, prices_json, distance_km,
                    route_description, source_url, raw_evidence_hash,
                    fetched_at, expires_at, parser_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    lookup.source,
                    lookup.entry_name,
                    entry_normalized,
                    lookup.exit_name,
                    exit_normalized,
                    json.dumps(prices, ensure_ascii=False, sort_keys=True),
                    lookup.distance_km,
                    lookup.route_label,
                    lookup.source_url,
                    lookup.raw_evidence_hash,
                    lookup.fetched_at.isoformat(),
                    expires_at.isoformat(),
                    lookup.parser_version,
                ),
            )
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise TollCacheError("The local toll cache could not be written.") from error
        finally:
            connection.close()
