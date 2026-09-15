"""SQLite cache with independent metadata and offer TTLs for V0.6."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backend.accommodation.models import (
    AccommodationOffer,
    AccommodationResult,
    AccommodationSearchRequest,
    PlaceRecord,
)
from crawler.accommodation.parser import PARSER_VERSION


class AccommodationCacheError(RuntimeError):
    """The accommodation cache could not be read or written safely."""


@dataclass(frozen=True)
class CachedAccommodationSearch:
    results: list[AccommodationResult]
    complete: bool
    fetched_at: datetime
    fresh: bool


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accommodation_source_places (
    place_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT,
    source_url TEXT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    lat REAL,
    lng REAL,
    address TEXT,
    rating REAL,
    rating_scale REAL,
    review_count INTEGER,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    parser_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accommodation_places_expiry
    ON accommodation_source_places(expires_at);
CREATE INDEX IF NOT EXISTS idx_accommodation_places_identity
    ON accommodation_source_places(source, source_id);

CREATE TABLE IF NOT EXISTS accommodation_offers (
    offer_cache_key TEXT PRIMARY KEY,
    search_cache_key TEXT NOT NULL,
    result_index INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_offer_id TEXT,
    place_key TEXT NOT NULL,
    place_source_id TEXT,
    checkin TEXT NOT NULL,
    checkout TEXT NOT NULL,
    adults INTEGER NOT NULL,
    children INTEGER NOT NULL,
    room_name TEXT,
    base_price_krw INTEGER,
    taxes_krw INTEGER,
    final_price_krw INTEGER,
    price_basis TEXT NOT NULL DEFAULT 'UNKNOWN',
    price_freshness TEXT NOT NULL DEFAULT 'unknown',
    availability INTEGER,
    distance_km REAL,
    distance_text TEXT,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    parser_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accommodation_offers_search
    ON accommodation_offers(search_cache_key, result_index);
CREATE INDEX IF NOT EXISTS idx_accommodation_offers_expiry
    ON accommodation_offers(expires_at);

CREATE TABLE IF NOT EXISTS accommodation_search_cache (
    search_cache_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    complete INTEGER NOT NULL,
    result_count INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    parser_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accommodation_search_expiry
    ON accommodation_search_cache(expires_at);
"""


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    return _utc_datetime(datetime.fromisoformat(value))


def _iso_date(value: date) -> str:
    return value.isoformat()


def make_search_cache_key(
    source: str,
    request: AccommodationSearchRequest,
) -> str:
    identity = {
        "source": source,
        "destination": {
            "lat": request.destination.lat,
            "lng": request.destination.lng,
            "label": request.destination.label,
        },
        "checkin": _iso_date(request.checkin),
        "checkout": _iso_date(request.checkout),
        "adults": request.adults,
        "children": request.children,
        "radius_km": request.radius_km,
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _place_key(place: PlaceRecord) -> str:
    if place.source_id:
        identity = f"{place.source}|id|{place.source_id}"
    else:
        identity = "|".join(
            (place.source, "url", place.source_url or "", place.name)
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _offer_key(
    search_key: str,
    place_key: str,
    offer: AccommodationOffer,
    result_index: int,
    offer_index: int,
) -> str:
    identity = "|".join(
        (
            search_key,
            place_key,
            offer.source_offer_id or "",
            str(result_index),
            str(offer_index),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class AccommodationCache:
    """Short-lived date offer cache plus longer-lived place metadata cache."""

    def __init__(
        self,
        database_path: Path,
        *,
        place_ttl_s: float = 3 * 24 * 60 * 60,
        offer_ttl_s: float = 3 * 60 * 60,
    ) -> None:
        self.database_path = Path(database_path)
        self.place_ttl = timedelta(seconds=max(0.0, float(place_ttl_s)))
        self.offer_ttl = timedelta(seconds=max(0.0, float(offer_ttl_s)))

    def _connection(self) -> sqlite3.Connection:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.database_path))
            connection.row_factory = sqlite3.Row
            connection.executescript(SCHEMA_SQL)
            self._migrate_offer_columns(connection)
            return connection
        except (OSError, sqlite3.Error) as error:
            raise AccommodationCacheError(
                "The local accommodation cache database is unavailable."
            ) from error

    @staticmethod
    def _migrate_offer_columns(connection: sqlite3.Connection) -> None:
        """Add V0.9 offer metadata without deleting existing cache rows."""

        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(accommodation_offers)").fetchall()
        }
        if "price_basis" not in columns:
            connection.execute(
                "ALTER TABLE accommodation_offers ADD COLUMN price_basis TEXT NOT NULL DEFAULT 'UNKNOWN'"
            )
        if "price_freshness" not in columns:
            connection.execute(
                "ALTER TABLE accommodation_offers ADD COLUMN price_freshness TEXT NOT NULL DEFAULT 'unknown'"
            )
        connection.commit()

    def get(
        self,
        source: str,
        request: AccommodationSearchRequest,
        *,
        allow_stale: bool = False,
    ) -> CachedAccommodationSearch | None:
        search_key = make_search_cache_key(source, request)
        connection = self._connection()
        try:
            search_row = connection.execute(
                """
                SELECT complete, result_count, fetched_at, expires_at, parser_version
                FROM accommodation_search_cache
                WHERE search_cache_key = ? AND source = ?
                """,
                (search_key, source),
            ).fetchone()
            if search_row is None or search_row["parser_version"] != PARSER_VERSION:
                return None

            fetched_at = _parse_datetime(search_row["fetched_at"])
            expires_at = _parse_datetime(search_row["expires_at"])
            fresh = expires_at > datetime.now(timezone.utc)
            if not fresh and not allow_stale:
                return None

            rows = connection.execute(
                """
                SELECT
                    o.result_index, o.source, o.source_offer_id,
                    o.place_source_id, o.checkin, o.checkout,
                    o.adults, o.children, o.room_name,
                    o.base_price_krw, o.taxes_krw, o.final_price_krw,
                    o.price_basis, o.price_freshness,
                    o.availability, o.distance_km, o.distance_text,
                    o.fetched_at AS offer_fetched_at, o.expires_at AS offer_expires_at,
                    o.parser_version AS offer_parser_version,
                    p.source AS place_source, p.source_id, p.source_url,
                    p.name, p.category, p.lat, p.lng, p.address,
                    p.rating, p.rating_scale, p.review_count,
                    p.fetched_at AS place_fetched_at, p.expires_at AS place_expires_at,
                    p.parser_version AS place_parser_version
                FROM accommodation_offers AS o
                JOIN accommodation_source_places AS p ON p.place_key = o.place_key
                WHERE o.search_cache_key = ?
                ORDER BY o.result_index, o.offer_cache_key
                """,
                (search_key,),
            ).fetchall()
            if int(search_row["result_count"]) == 0:
                return CachedAccommodationSearch([], bool(search_row["complete"]), fetched_at, fresh)
            if len(rows) == 0:
                return None

            results_by_index: dict[int, AccommodationResult] = {}
            for row in rows:
                if (
                    row["offer_parser_version"] != PARSER_VERSION
                    or row["place_parser_version"] != PARSER_VERSION
                ):
                    return None
                offer_expires_at = _parse_datetime(row["offer_expires_at"])
                place_expires_at = _parse_datetime(row["place_expires_at"])
                if not allow_stale and (
                    offer_expires_at <= datetime.now(timezone.utc)
                    or place_expires_at <= datetime.now(timezone.utc)
                ):
                    return None
                place = PlaceRecord(
                    source=row["place_source"],
                    source_id=row["source_id"],
                    source_url=row["source_url"],
                    name=row["name"],
                    category=row["category"],
                    lat=row["lat"],
                    lng=row["lng"],
                    address=row["address"],
                    rating=row["rating"],
                    rating_scale=row["rating_scale"],
                    review_count=row["review_count"],
                    fetched_at=_parse_datetime(row["place_fetched_at"]),
                )
                offer = AccommodationOffer(
                    source=row["source"],
                    source_offer_id=row["source_offer_id"],
                    place_source_id=row["place_source_id"],
                    checkin=date.fromisoformat(row["checkin"]),
                    checkout=date.fromisoformat(row["checkout"]),
                    adults=row["adults"],
                    children=row["children"],
                    room_name=row["room_name"],
                    base_price_krw=row["base_price_krw"],
                    taxes_krw=row["taxes_krw"],
                    final_price_krw=row["final_price_krw"],
                    price_basis=row["price_basis"],
                    price_freshness=(
                        "expired"
                        if row["offer_expires_at"]
                        and _parse_datetime(row["offer_expires_at"]) <= datetime.now(timezone.utc)
                        else row["price_freshness"]
                    ),
                    expires_at=_parse_datetime(row["offer_expires_at"]),
                    availability=(
                        None
                        if row["availability"] is None
                        else bool(row["availability"])
                    ),
                    fetched_at=_parse_datetime(row["offer_fetched_at"]),
                )
                index = int(row["result_index"])
                existing = results_by_index.get(index)
                if existing is None:
                    results_by_index[index] = AccommodationResult(
                        place=place,
                        offers=[offer],
                        distance_km=row["distance_km"],
                        distance_text=row["distance_text"],
                    )
                else:
                    results_by_index[index] = existing.model_copy(
                        update={"offers": [*existing.offers, offer]}
                    )
            results = [results_by_index[index] for index in sorted(results_by_index)]
            if len(results) != int(search_row["result_count"]):
                return None
            return CachedAccommodationSearch(
                results=results,
                complete=bool(search_row["complete"]),
                fetched_at=fetched_at,
                fresh=fresh,
            )
        except AccommodationCacheError:
            raise
        except (sqlite3.Error, TypeError, ValueError, KeyError) as error:
            raise AccommodationCacheError(
                "The local accommodation cache contains malformed data."
            ) from error
        finally:
            connection.close()

    def put(
        self,
        source: str,
        request: AccommodationSearchRequest,
        results: list[AccommodationResult],
        *,
        complete: bool,
    ) -> None:
        search_key = make_search_cache_key(source, request)
        now = datetime.now(timezone.utc)
        place_expires_at = now + self.place_ttl
        offer_expires_at = now + self.offer_ttl
        connection = self._connection()
        try:
            for result_index, result in enumerate(results):
                place = result.place
                place_key = _place_key(place)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO accommodation_source_places(
                        place_key, source, source_id, source_url, name, category,
                        lat, lng, address, rating, rating_scale, review_count,
                        fetched_at, expires_at, parser_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        place_key,
                        place.source,
                        place.source_id,
                        place.source_url,
                        place.name,
                        place.category,
                        place.lat,
                        place.lng,
                        place.address,
                        place.rating,
                        place.rating_scale,
                        place.review_count,
                        _utc_datetime(place.fetched_at).isoformat(),
                        place_expires_at.isoformat(),
                        PARSER_VERSION,
                    ),
                )
                for offer_index, offer in enumerate(result.offers):
                    offer_key = _offer_key(
                        search_key,
                        place_key,
                        offer,
                        result_index,
                        offer_index,
                    )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO accommodation_offers(
                            offer_cache_key, search_cache_key, result_index,
                            source, source_offer_id, place_key, place_source_id,
                            checkin, checkout, adults, children, room_name,
                            base_price_krw, taxes_krw, final_price_krw, availability,
                            price_basis, price_freshness,
                            distance_km, distance_text, fetched_at, expires_at,
                            parser_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            offer_key,
                            search_key,
                            result_index,
                            offer.source,
                            offer.source_offer_id,
                            place_key,
                            offer.place_source_id,
                            _iso_date(offer.checkin),
                            _iso_date(offer.checkout),
                            offer.adults,
                            offer.children,
                            offer.room_name,
                            offer.base_price_krw,
                            offer.taxes_krw,
                            offer.final_price_krw,
                            None if offer.availability is None else int(offer.availability),
                            offer.price_basis.value,
                            offer.price_freshness,
                            result.distance_km,
                            result.distance_text,
                            _utc_datetime(offer.fetched_at).isoformat(),
                            offer_expires_at.isoformat(),
                            PARSER_VERSION,
                        ),
                    )
            connection.execute(
                """
                INSERT OR REPLACE INTO accommodation_search_cache(
                    search_cache_key, source, complete, result_count,
                    fetched_at, expires_at, parser_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    search_key,
                    source,
                    int(complete),
                    len(results),
                    now.isoformat(),
                    offer_expires_at.isoformat(),
                    PARSER_VERSION,
                ),
            )
            connection.commit()
        except (sqlite3.Error, TypeError, ValueError) as error:
            connection.rollback()
            raise AccommodationCacheError(
                "The local accommodation cache could not be written."
            ) from error
        finally:
            connection.close()
