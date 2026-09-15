"""SQLite persistence for derived V0.8 entities and explainability data."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.entities.models import (
    CanonicalPlace,
    EntityResolveResponse,
    MatchDecision,
)

_JOURNAL_MODE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_places (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    lat REAL,
    lng REAL,
    address TEXT,
    normalized_rating REAL,
    rating_confidence REAL,
    review_count_total INTEGER,
    observed_review_count_sum INTEGER,
    review_count_semantics TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    confidence REAL NOT NULL,
    aliases_json TEXT NOT NULL,
    raw_fields_json TEXT NOT NULL,
    coordinate_conflict INTEGER NOT NULL,
    rating_disagreement INTEGER NOT NULL,
    decision_trace_json TEXT NOT NULL,
    matcher_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canonical_place_members (
    canonical_id TEXT NOT NULL REFERENCES canonical_places(id) ON DELETE CASCADE,
    source_record_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT,
    source_url TEXT,
    source_record_fingerprint TEXT NOT NULL,
    match_score REAL NOT NULL,
    match_method TEXT NOT NULL,
    source_confidence REAL NOT NULL,
    merged_at TEXT NOT NULL,
    raw_record_json TEXT NOT NULL,
    PRIMARY KEY (canonical_id, source_record_id)
);

CREATE TABLE IF NOT EXISTS entity_match_candidates (
    record_a TEXT NOT NULL,
    record_b TEXT NOT NULL,
    score REAL NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    matcher_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    name_score REAL NOT NULL,
    address_score REAL,
    coordinate_score REAL,
    category_score REAL NOT NULL,
    source_identity_score REAL NOT NULL,
    distance_m REAL,
    method TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    PRIMARY KEY (record_a, record_b, matcher_version)
);

CREATE TABLE IF NOT EXISTS entity_match_overrides (
    record_a TEXT NOT NULL,
    record_b TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (record_a, record_b)
);

CREATE TABLE IF NOT EXISTS canonical_place_offers (
    canonical_id TEXT NOT NULL REFERENCES canonical_places(id) ON DELETE CASCADE,
    offer_id TEXT NOT NULL,
    offer_json TEXT NOT NULL,
    PRIMARY KEY (canonical_id, offer_id)
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _pair(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first < second else (second, first)


class EntityResolutionRepository:
    """A separate derived DB; V0.6/V0.7 raw cache databases are untouched."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        # SQLite can reject concurrent journal-mode negotiation even when the
        # connection has a busy timeout.  Serialize this process-local setup;
        # the write transaction below remains protected by SQLite's timeout.
        with _JOURNAL_MODE_LOCK:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @staticmethod
    def _canonical_row(place: CanonicalPlace) -> tuple[Any, ...]:
        return (
            place.id,
            place.name,
            place.category,
            place.lat,
            place.lng,
            place.address,
            place.normalized_rating,
            place.rating_confidence,
            place.review_count_total,
            place.observed_review_count_sum,
            place.review_count_semantics,
            place.source_count,
            place.confidence,
            _json(place.aliases),
            _json(place.raw_fields),
            int(place.coordinate_conflict),
            int(place.rating_disagreement),
            _json([trace.model_dump(mode="json") for trace in place.decision_trace]),
            place.matcher_version,
            place.created_at.isoformat(),
            place.updated_at.isoformat(),
        )

    def persist(self, result: EntityResolveResponse, *, replace: bool = True) -> None:
        """Persist derived rows transactionally; raw source tables are never deleted."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if replace:
                connection.execute("DELETE FROM canonical_place_offers")
                connection.execute("DELETE FROM canonical_place_members")
                connection.execute("DELETE FROM canonical_places")
                connection.execute("DELETE FROM entity_match_candidates")
            connection.executemany(
                """
                INSERT OR REPLACE INTO canonical_places (
                    id, name, category, lat, lng, address, normalized_rating,
                    rating_confidence, review_count_total, observed_review_count_sum,
                    review_count_semantics, source_count, confidence, aliases_json,
                    raw_fields_json, coordinate_conflict, rating_disagreement,
                    decision_trace_json, matcher_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._canonical_row(place) for place in result.canonical_places],
            )
            for place in result.canonical_places:
                for member in place.sources:
                    source_record = result.source_records.get(member.source_record_id)
                    raw_payload = (
                        source_record.model_dump(mode="json")
                        if source_record is not None
                        else {
                            "source": member.source,
                            "source_id": member.source_id,
                            "source_url": member.source_url,
                            "source_record_id": member.source_record_id,
                            "source_record_fingerprint": member.source_record_fingerprint,
                        }
                    )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO canonical_place_members (
                            canonical_id, source_record_id, source, source_id,
                            source_url, source_record_fingerprint, match_score,
                            match_method, source_confidence, merged_at, raw_record_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            place.id,
                            member.source_record_id,
                            member.source,
                            member.source_id,
                            member.source_url,
                            member.source_record_fingerprint,
                            member.match_score,
                            member.match_method,
                            member.source_confidence,
                            member.merged_at.isoformat(),
                            _json(raw_payload),
                        ),
                    )
            connection.executemany(
                """
                INSERT OR REPLACE INTO entity_match_candidates (
                    record_a, record_b, score, decision, reason, matcher_version,
                    created_at, name_score, address_score, coordinate_score,
                    category_score, source_identity_score, distance_m, method, signals_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        candidate.record_a,
                        candidate.record_b,
                        candidate.score,
                        candidate.decision.value,
                        candidate.reason,
                        candidate.matcher_version,
                        candidate.created_at.isoformat(),
                        candidate.name_score,
                        candidate.address_score,
                        candidate.coordinate_score,
                        candidate.category_score,
                        candidate.source_identity_score,
                        candidate.distance_m,
                        candidate.method,
                        _json(candidate.model_dump(mode="json")),
                    )
                    for candidate in result.candidates
                ],
            )
            offer_rows: list[tuple[str, str, str]] = []
            for canonical_id, offers in result.offers_by_canonical_id.items():
                for offer in offers:
                    payload = offer.model_dump(mode="json")
                    offer_id = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:32]
                    offer_rows.append((canonical_id, offer_id, _json(payload)))
            connection.executemany(
                "INSERT OR REPLACE INTO canonical_place_offers (canonical_id, offer_id, offer_json) VALUES (?, ?, ?)",
                offer_rows,
            )
            connection.commit()

    def list_canonical(self, *, category: str | None = None, limit: int = 500) -> list[CanonicalPlace]:
        limit = max(1, min(5000, int(limit)))
        with self._connect() as connection:
            if category:
                rows = connection.execute(
                    "SELECT * FROM canonical_places WHERE category = ? ORDER BY id LIMIT ?",
                    (category, limit),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM canonical_places ORDER BY id LIMIT ?", (limit,)).fetchall()
            result: list[CanonicalPlace] = []
            for row in rows:
                payload = {
                    "id": row["id"],
                    "name": row["name"],
                    "category": row["category"],
                    "lat": row["lat"],
                    "lng": row["lng"],
                    "address": row["address"],
                    "normalized_rating": row["normalized_rating"],
                    "rating_confidence": row["rating_confidence"],
                    "review_count_total": row["review_count_total"],
                    "observed_review_count_sum": row["observed_review_count_sum"],
                    "review_count_semantics": row["review_count_semantics"],
                    "source_count": row["source_count"],
                    "confidence": row["confidence"],
                    "aliases": json.loads(row["aliases_json"]),
                    "raw_fields": json.loads(row["raw_fields_json"]),
                    "coordinate_conflict": bool(row["coordinate_conflict"]),
                    "rating_disagreement": bool(row["rating_disagreement"]),
                    "decision_trace": json.loads(row["decision_trace_json"]),
                    "matcher_version": row["matcher_version"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                member_rows = connection.execute(
                    """
                    SELECT source, source_id, source_url, source_record_id,
                           source_record_fingerprint, match_score, match_method,
                           source_confidence, merged_at
                    FROM canonical_place_members
                    WHERE canonical_id = ? ORDER BY source_record_id
                    """,
                    (row["id"],),
                ).fetchall()
                payload["sources"] = [
                    {
                        "source": member["source"],
                        "source_id": member["source_id"],
                        "source_url": member["source_url"],
                        "source_record_id": member["source_record_id"],
                        "source_record_fingerprint": member["source_record_fingerprint"],
                        "match_score": member["match_score"],
                        "match_method": member["match_method"],
                        "source_confidence": member["source_confidence"],
                        "merged_at": member["merged_at"],
                    }
                    for member in member_rows
                ]
                result.append(CanonicalPlace.model_validate(payload))
            return result

    def get_canonical(self, canonical_id: str) -> CanonicalPlace | None:
        return next((place for place in self.list_canonical(limit=5000) if place.id == canonical_id), None)

    def debug(self, canonical_id: str) -> dict[str, Any] | None:
        place = self.get_canonical(canonical_id)
        if place is None:
            return None
        ids = [member.source_record_id for member in place.sources]
        with self._connect() as connection:
            members = connection.execute(
                "SELECT * FROM canonical_place_members WHERE canonical_id = ? ORDER BY source_record_id",
                (canonical_id,),
            ).fetchall()
            candidates = []
            for row in connection.execute(
                "SELECT * FROM entity_match_candidates ORDER BY record_a, record_b"
            ).fetchall():
                if row["record_a"] in ids and row["record_b"] in ids:
                    candidates.append(
                        {
                            "record_a": row["record_a"],
                            "record_b": row["record_b"],
                            "score": row["score"],
                            "decision": row["decision"],
                            "reason": row["reason"],
                            "method": row["method"],
                            "signals": json.loads(row["signals_json"]),
                        }
                    )
            return {
                "canonical": place.model_dump(mode="json"),
                "members": [json.loads(row["raw_record_json"]) for row in members],
                "decision_trace": candidates,
            }

    def save_override(
        self,
        record_a: str,
        record_b: str,
        decision: MatchDecision,
        *,
        reason: str,
        updated_at: datetime | str,
    ) -> None:
        first, second = _pair(record_a, record_b)
        timestamp = updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO entity_match_overrides
                    (record_a, record_b, decision, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (first, second, decision.value, reason, timestamp),
            )
            connection.commit()

    def load_overrides(self) -> dict[tuple[str, str], MatchDecision]:
        with self._connect() as connection:
            rows = connection.execute("SELECT record_a, record_b, decision FROM entity_match_overrides").fetchall()
        return {
            (row["record_a"], row["record_b"]): MatchDecision(row["decision"])
            for row in rows
        }

    def close(self) -> None:
        """Connections are per operation, so there is no long-lived handle."""


__all__ = ["SCHEMA", "EntityResolutionRepository"]
