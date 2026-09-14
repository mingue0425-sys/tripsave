"""SQLite cache for parsed official toll-page results."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
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

    @property
    def state(self) -> str:
        """Expose the explicit cache lifecycle used by the service."""

        return "FRESH" if self.fresh else "STALE_USABLE"


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

    def __init__(
        self,
        database_path: Path,
        *,
        ttl_days: int,
        stale_max_age_days: int = 180,
    ) -> None:
        self.database_path = Path(database_path)
        self.ttl = timedelta(days=max(1, ttl_days))
        self.stale_max_age = timedelta(days=max(1, stale_max_age_days))
        # Schema creation is a one-time database lifecycle operation, not a
        # cache-read operation.  Repeating executescript/DDL on every lookup
        # was visible in the warm aggregate timing (and caused avoidable DB
        # lock contention with the official station writer).
        self._schema_lock = threading.Lock()
        self._schema_initialized = False
        self._station_alias_ids: dict[str, set[str]] | None = None

    def _connection(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = connect_database(str(self.database_path))
            with self._schema_lock:
                if not self._schema_initialized:
                    initialize_schema(connection)
                    self._schema_initialized = True
            return connection
        except (OSError, sqlite3.Error) as error:
            if connection is not None:
                connection.close()
            raise TollCacheError("The local toll cache database is unavailable.") from error

    def _load_station_alias_ids(
        self, connection: sqlite3.Connection, *, force: bool = False
    ) -> dict[str, set[str]]:
        if self._station_alias_ids is not None and not force:
            return self._station_alias_ids
        rows = connection.execute(
            "SELECT official_id, normalized_name, aliases_json FROM official_stations"
        ).fetchall()
        mapping: dict[str, set[str]] = {}
        for row in rows:
            official_id = str(row[0] or "")
            if not official_id:
                continue
            values = [row[1]]
            try:
                aliases = json.loads(row[2] or "[]")
            except (TypeError, json.JSONDecodeError):
                aliases = []
            values.extend(alias for alias in aliases if isinstance(alias, str))
            for value in values:
                normalized = normalize_toll_name(value)
                if normalized:
                    mapping.setdefault(normalized, set()).add(official_id)
        self._station_alias_ids = mapping
        return mapping

    def _remember_station_aliases(
        self,
        connection: sqlite3.Connection,
        values: list[str | None],
        official_id: str | None,
    ) -> None:
        if not official_id:
            return
        mapping = self._load_station_alias_ids(connection)
        for value in values:
            normalized = normalize_toll_name(value or "")
            if normalized:
                mapping.setdefault(normalized, set()).add(str(official_id))

    def resolve_official_id(self, station_name: str) -> str | None:
        """Resolve one verified alias without crawling the official station list."""

        normalized = normalize_toll_name(station_name)
        if not normalized:
            return None
        connection = self._connection()
        try:
            mapping = self._load_station_alias_ids(connection)
            matches = mapping.get(normalized, set())
            # OfficialStationStore may have learned an alias through a
            # separate connection after this cache instance loaded its small
            # in-memory dictionary.  Refresh only on a miss/ambiguous match;
            # normal warm reads stay memory-only.
            if len(matches) != 1:
                mapping = self._load_station_alias_ids(connection, force=True)
        except sqlite3.Error as error:
            raise TollCacheError("The local toll cache could not resolve a station.") from error
        finally:
            connection.close()
        matches = mapping.get(normalized, set())
        return next(iter(matches)) if len(matches) == 1 else None

    @staticmethod
    def _as_utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _decode_row(
        self, row: sqlite3.Row | tuple[object, ...] | None, *, allow_stale: bool
    ) -> TollCacheEntry | None:
        if row is None:
            return None
        if row[12] != PARSER_VERSION:
            # A parser change can alter the meaning of a previously stored
            # HTML result.  It is safer to recrawl than to present an older
            # interpretation as current or stale evidence.
            return None
        try:
            fetched_at = self._as_utc(str(row[10]))
            expires_at = self._as_utc(str(row[11]))
            prices = {
                key: int(value)
                for key, value in json.loads(str(row[5])).items()
            }
            lookup = OfficialTollLookup(
                source=row[0],
                entry_name=row[1],
                entry_official_id=row[2],
                exit_name=row[3],
                exit_official_id=row[4],
                route_label=row[7] or (str(row[1]) + "~" + str(row[3])),
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
        now = datetime.now(timezone.utc)
        fresh = expires_at > now
        if not fresh and now - fetched_at > self.stale_max_age:
            # A verified value is useful for a bounded outage window, but it
            # must not become an indefinitely trusted official price.
            return None
        if not fresh and not allow_stale:
            return None
        return TollCacheEntry(lookup=lookup, fresh=fresh)

    @staticmethod
    def _cache_select_columns() -> str:
        return """
            SELECT source, entry_name, entry_official_id, exit_name,
                   exit_official_id, prices_json, distance_km,
                   route_description, source_url, raw_evidence_hash,
                   fetched_at, expires_at, parser_version
            FROM toll_rates_cache
        """

    def get_many(
        self,
        pairs: list[tuple[str, str]],
        *,
        allow_stale: bool = False,
    ) -> list[TollCacheEntry | None]:
        """Read several directional pairs through one SQLite connection.

        Route analysis can produce multiple candidate pairs before official
        validation.  Opening a connection and loading the alias dictionary
        for every candidate made a warm cache hit look like a slow source
        lookup.  Keep the candidate order, but amortize the database work.
        """

        if not pairs:
            return []
        connection = self._connection()
        try:
            columns = self._cache_select_columns()
            alias_mapping = self._load_station_alias_ids(connection)
            alias_refreshed = False
            rows: list[sqlite3.Row | tuple[object, ...] | None] = []
            for entry_name, exit_name in pairs:
                cache_key, entry_normalized, exit_normalized = _cache_key(
                    entry_name, exit_name
                )
                row = connection.execute(
                    columns
                    + """
                    WHERE cache_key = ? AND entry_normalized = ? AND exit_normalized = ?
                      AND entry_official_id IS NULL AND exit_official_id IS NULL
                    """,
                    (cache_key, entry_normalized, exit_normalized),
                ).fetchone()

                entry_matches = alias_mapping.get(entry_normalized, set())
                exit_matches = alias_mapping.get(exit_normalized, set())
                if row is None and (len(entry_matches) != 1 or len(exit_matches) != 1):
                    if not alias_refreshed:
                        alias_mapping = self._load_station_alias_ids(
                            connection, force=True
                        )
                        alias_refreshed = True
                        entry_matches = alias_mapping.get(entry_normalized, set())
                        exit_matches = alias_mapping.get(exit_normalized, set())
                if row is None and len(entry_matches) == 1 and len(exit_matches) == 1:
                    resolved_entry_id = next(iter(entry_matches))
                    resolved_exit_id = next(iter(exit_matches))
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
                if row is None:
                    row = connection.execute(
                        columns
                        + """
                        WHERE entry_normalized = ? AND exit_normalized = ?
                        ORDER BY fetched_at DESC
                        LIMIT 1
                        """,
                        (entry_normalized, exit_normalized),
                    ).fetchone()
                rows.append(row)
        except sqlite3.Error as error:
            raise TollCacheError("The local toll cache could not be queried.") from error
        finally:
            connection.close()
        return [self._decode_row(row, allow_stale=allow_stale) for row in rows]

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
                station_alias_ids = self._load_station_alias_ids(connection)
                entry_matches = station_alias_ids.get(entry_normalized, set())
                exit_matches = station_alias_ids.get(exit_normalized, set())
                if len(entry_matches) != 1 or len(exit_matches) != 1:
                    station_alias_ids = self._load_station_alias_ids(connection, force=True)
                    entry_matches = station_alias_ids.get(entry_normalized, set())
                    exit_matches = station_alias_ids.get(exit_normalized, set())
                resolved_entry_id = next(iter(entry_matches)) if len(entry_matches) == 1 else None
                resolved_exit_id = next(iter(exit_matches)) if len(exit_matches) == 1 else None
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
        return self._decode_row(row, allow_stale=allow_stale)

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
            self._remember_station_aliases(
                connection,
                [entry_name, lookup.entry_name],
                entry_official_id,
            )
            self._remember_station_aliases(
                connection,
                [exit_name, lookup.exit_name],
                exit_official_id,
            )
        except sqlite3.Error as error:
            connection.rollback()
            raise TollCacheError("The local toll cache could not be written.") from error
        finally:
            connection.close()
