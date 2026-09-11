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


def _cache_key(
    entry_name: str,
    exit_name: str,
    *,
    entry_official_id: str | None = None,
    exit_official_id: str | None = None,
) -> tuple[str, str, str]:
    entry_normalized = normalize_toll_name(entry_name)
    exit_normalized = normalize_toll_name(exit_name)
    if not entry_normalized or not exit_normalized:
        raise TollCacheError("Toll cache keys require both normalized gate names.")
    # Prefer the IDs returned by the official station validation flow.  The
    # normalized names remain part of the row and are checked on reads so a
    # stale or malformed identity cannot silently alias another station.
    entry_identity = entry_official_id or entry_normalized
    exit_identity = exit_official_id or exit_normalized
    raw_key = f"official_web|{entry_identity}|{exit_identity}"
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

    @staticmethod
    def _as_utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def get(
        self,
        entry_name: str,
        exit_name: str,
        *,
        allow_stale: bool = False,
        entry_official_id: str | None = None,
        exit_official_id: str | None = None,
    ) -> TollCacheEntry | None:
        cache_key, entry_normalized, exit_normalized = _cache_key(
            entry_name,
            exit_name,
            entry_official_id=entry_official_id,
            exit_official_id=exit_official_id,
        )
        connection = self._connection()
        try:
            columns = """
                SELECT source, entry_name, entry_official_id, exit_name,
                       exit_official_id, prices_json, distance_km,
                       route_description, source_url, raw_evidence_hash,
                       fetched_at, expires_at, parser_version
                FROM toll_rates_cache
            """
            if entry_official_id is not None or exit_official_id is not None:
                # When IDs are supplied, they are the identity.  The display
                # name can legitimately be an observed OSM alias and must not
                # make an ID-keyed row miss.
                row = connection.execute(
                    columns
                    + """
                    WHERE cache_key = ?
                      AND (
                        (? IS NULL AND entry_official_id IS NULL)
                        OR (? IS NOT NULL AND entry_official_id = ?)
                      )
                      AND (
                        (? IS NULL AND exit_official_id IS NULL)
                        OR (? IS NOT NULL AND exit_official_id = ?)
                      )
                    """,
                    (
                        cache_key,
                        entry_official_id,
                        entry_official_id,
                        entry_official_id,
                        exit_official_id,
                        exit_official_id,
                        exit_official_id,
                    ),
                ).fetchone()
            else:
                row = connection.execute(
                    columns
                    + """
                    WHERE cache_key = ? AND entry_normalized = ? AND exit_normalized = ?
                      AND entry_official_id IS NULL AND exit_official_id IS NULL
                    """,
                    (cache_key, entry_normalized, exit_normalized),
                ).fetchone()

            # The service normally checks the cache before launching a
            # browser, so it may not know the official IDs yet.  Resolve an
            # observed OSM alias through the persisted official station
            # dictionary, then read the canonical ID-keyed row.  Ambiguous
            # aliases are deliberately ignored and recrawled.
            if row is None and entry_official_id is None and exit_official_id is None:
                station_rows = connection.execute(
                    "SELECT official_id, normalized_name, aliases_json FROM official_stations"
                ).fetchall()

                def resolve_station_id(normalized: str) -> str | None:
                    matches: set[str] = set()
                    for station_row in station_rows:
                        official_id = str(station_row[0] or "")
                        if not official_id:
                            continue
                        if station_row[1] == normalized:
                            matches.add(official_id)
                            continue
                        try:
                            aliases = json.loads(station_row[2] or "[]")
                        except (TypeError, json.JSONDecodeError):
                            aliases = []
                        if any(
                            normalize_toll_name(alias) == normalized
                            for alias in aliases
                            if isinstance(alias, str)
                        ):
                            matches.add(official_id)
                    return next(iter(matches)) if len(matches) == 1 else None

                resolved_entry_id = resolve_station_id(entry_normalized)
                resolved_exit_id = resolve_station_id(exit_normalized)
                if resolved_entry_id and resolved_exit_id:
                    resolved_key, _, _ = _cache_key(
                        entry_name,
                        exit_name,
                        entry_official_id=resolved_entry_id,
                        exit_official_id=resolved_exit_id,
                    )
                    row = connection.execute(
                        columns
                        + """
                        WHERE cache_key = ?
                          AND entry_official_id = ?
                          AND exit_official_id = ?
                        """,
                        (resolved_key, resolved_entry_id, resolved_exit_id),
                    ).fetchone()

            if row is None and entry_official_id is None and exit_official_id is None:
                row = connection.execute(
                """
                SELECT source, entry_name, entry_official_id, exit_name,
                       exit_official_id, prices_json, distance_km,
                       route_description, source_url, raw_evidence_hash,
                       fetched_at, expires_at, parser_version
                FROM toll_rates_cache
                WHERE entry_normalized = ? AND exit_normalized = ?
                ORDER BY fetched_at DESC
                LIMIT 1
                """,
                (entry_normalized, exit_normalized),
                ).fetchone()
        except sqlite3.Error as error:
            raise TollCacheError("The local toll cache could not be queried.") from error
        finally:
            connection.close()
        if row is None:
            return None
        if row[12] != PARSER_VERSION:
            # A parser change can alter the meaning of a previously stored
            # HTML result.  It is safer to recrawl than to present an older
            # interpretation as current or stale evidence.
            return None
        try:
            fetched_at = self._as_utc(row[10])
            expires_at = self._as_utc(row[11])
            prices = {
                key: int(value)
                for key, value in json.loads(row[5]).items()
            }
            lookup = OfficialTollLookup(
                source=row[0],
                entry_name=row[1],
                entry_official_id=row[2],
                exit_name=row[3],
                exit_official_id=row[4],
                route_label=row[7] or (row[1] + "~" + row[3]),
                route_stops=[],
                distance_km=row[6],
                prices=prices,
                source_url=row[8],
                fetched_at=fetched_at,
                raw_evidence_hash=row[9],
                parser_version=row[12] or PARSER_VERSION,
            )
        except (
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
            json.JSONDecodeError,
        ) as error:
            raise TollCacheError("The local toll cache contains malformed data.") from error
        fresh = expires_at > datetime.now(timezone.utc)
        if not fresh and not allow_stale:
            return None
        return TollCacheEntry(lookup=lookup, fresh=fresh)

    def put(
        self,
        entry_name: str,
        exit_name: str,
        lookup: OfficialTollLookup,
        *,
        entry_official_id: str | None = None,
        exit_official_id: str | None = None,
    ) -> None:
        entry_official_id = entry_official_id or lookup.entry_official_id
        exit_official_id = exit_official_id or lookup.exit_official_id
        cache_key, entry_normalized, exit_normalized = _cache_key(
            entry_name,
            exit_name,
            entry_official_id=entry_official_id,
            exit_official_id=exit_official_id,
        )
        now = datetime.now(timezone.utc)
        expires_at = now + self.ttl
        prices = {key.value: value for key, value in lookup.prices.items()}
        connection = self._connection()
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO toll_rates_cache(
                    cache_key, source, entry_name, entry_normalized,
                    entry_official_id, exit_name, exit_normalized,
                    exit_official_id, prices_json, distance_km,
                    route_description, source_url, raw_evidence_hash,
                    fetched_at, expires_at, parser_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    lookup.source,
                    lookup.entry_name,
                    entry_normalized,
                    entry_official_id,
                    lookup.exit_name,
                    exit_normalized,
                    exit_official_id,
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
