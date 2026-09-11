"""SQLite cache for verified public-web fuel prices."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.fuel.models import FuelPriceResult, FuelType
from backend.fuel.parser import PARSER_VERSION
from backend.tolls.schema import connect_database, initialize_schema


class FuelCacheError(RuntimeError):
    """The local fuel cache could not be read or written."""


@dataclass(frozen=True)
class FuelCacheEntry:
    result: FuelPriceResult
    fresh: bool


def _cache_key(
    fuel_type: FuelType,
    *,
    scope: str = "national_average",
    region: str | None = None,
) -> str:
    identity = "|".join((fuel_type.value, scope, region or "", "krw_per_l"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class FuelPriceCache:
    """Short-TTL cache sharing the project's existing SQLite database."""

    def __init__(self, database_path: Path, *, ttl_s: float) -> None:
        self.database_path = Path(database_path)
        self.ttl = timedelta(seconds=max(60.0, float(ttl_s)))

    def _connection(self) -> sqlite3.Connection:
        try:
            connection = connect_database(str(self.database_path))
            initialize_schema(connection)
            return connection
        except (OSError, sqlite3.Error) as error:
            raise FuelCacheError("The local fuel price cache is unavailable.") from error

    @staticmethod
    def _as_utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def get(
        self,
        fuel_type: FuelType,
        *,
        scope: str = "national_average",
        region: str | None = None,
        allow_stale: bool = False,
    ) -> FuelCacheEntry | None:
        key = _cache_key(fuel_type, scope=scope, region=region)
        connection = self._connection()
        try:
            row = connection.execute(
                """
                SELECT fuel_type, scope, region, price_krw_per_l, unit,
                       source, source_url, observed_at, fetched_at, expires_at,
                       raw_evidence_hash, parser_version
                FROM fuel_prices_cache
                WHERE cache_key = ?
                """,
                (key,),
            ).fetchone()
        except sqlite3.Error as error:
            raise FuelCacheError("The local fuel price cache could not be queried.") from error
        finally:
            connection.close()
        if row is None:
            return None
        if row[11] != PARSER_VERSION:
            return None
        try:
            fetched_at = self._as_utc(row[8])
            expires_at = self._as_utc(row[9])
            observed_at = self._as_utc(row[7]) if row[7] else None
            fresh = expires_at > datetime.now(timezone.utc)
            if not fresh and not allow_stale:
                return None
            result = FuelPriceResult(
                fuel_type=FuelType(row[0]),
                price_krw_per_l=float(row[3]),
                unit=row[4],
                scope=row[1],
                region=row[2],
                source=row[5],
                source_url=row[6],
                observed_at=observed_at,
                fetched_at=fetched_at,
                expires_at=expires_at,
                source_status="fresh" if fresh else "stale",
                cache_hit=True,
                complete=True,
                raw_evidence_hash=row[10],
                parser_version=row[11],
            )
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            raise FuelCacheError("The local fuel price cache contains malformed data.") from error
        return FuelCacheEntry(result=result, fresh=fresh)

    def put(self, result: FuelPriceResult) -> None:
        if not result.complete or result.price_krw_per_l is None:
            raise FuelCacheError("Only complete fuel prices can be cached.")
        if result.raw_evidence_hash is None:
            raise FuelCacheError("A cached fuel price needs parser evidence.")
        key = _cache_key(result.fuel_type, scope=result.scope, region=result.region)
        now = datetime.now(timezone.utc)
        expires_at = now + self.ttl
        connection = self._connection()
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO fuel_prices_cache(
                    cache_key, fuel_type, scope, region, price_krw_per_l, unit,
                    source, source_url, observed_at, fetched_at, expires_at,
                    raw_evidence_hash, parser_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    result.fuel_type.value,
                    result.scope,
                    result.region,
                    result.price_krw_per_l,
                    result.unit.value,
                    result.source,
                    result.source_url,
                    result.observed_at.isoformat() if result.observed_at else None,
                    (result.fetched_at or now).isoformat(),
                    expires_at.isoformat(),
                    result.raw_evidence_hash,
                    result.parser_version,
                ),
            )
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise FuelCacheError("The local fuel price cache could not be written.") from error
        finally:
            connection.close()
