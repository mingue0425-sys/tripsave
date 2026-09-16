"""Durable, isolated storage for ephemeral trip-candidate sets.

The recommendation endpoint accepts opaque candidate-set identifiers rather
than trusting candidate data supplied by a browser.  Each operation opens its
own SQLite connection so separate request threads and Uvicorn worker
processes can use the same database safely.  Candidate rows are loaded in one
batch and validated through the canonical Pydantic model before ranking.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.trips.models import (
    TRIP_ENGINE_VERSION,
    TripCandidate,
    TripCandidateResponse,
)

LOGGER = logging.getLogger(__name__)

CANDIDATE_SET_SCHEMA_VERSION = "v1"
_CANDIDATE_SET_ID_PATTERN = re.compile(r"^cs_[A-Za-z0-9_-]{16,128}$")


class CandidateSetError(ValueError):
    """Base class for safe candidate-set lookup failures."""

    code = "CANDIDATE_SET_ERROR"
    http_status = 404

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CandidateSetNotFoundError(CandidateSetError):
    code = "CANDIDATE_SET_NOT_FOUND"
    http_status = 404


class CandidateSetExpiredError(CandidateSetError):
    code = "CANDIDATE_SET_EXPIRED"
    http_status = 410


class CandidateSetMismatchError(CandidateSetError):
    code = "CANDIDATE_SET_MISMATCH"
    http_status = 409


class CandidateSetFingerprintMismatchError(CandidateSetError):
    code = "CANDIDATE_SET_FINGERPRINT_MISMATCH"
    http_status = 409


class CandidateSetVersionMismatchError(CandidateSetError):
    code = "CANDIDATE_SET_VERSION_MISMATCH"
    http_status = 409


class CandidateSetCorruptError(CandidateSetError):
    code = "CANDIDATE_SET_INVALID"
    http_status = 500


class CandidateSetCapacityError(CandidateSetError):
    code = "CANDIDATE_SET_CAPACITY"
    http_status = 503


class CandidateSet(BaseModel):
    """A single isolated batch of assembled candidates."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=128)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: list[TripCandidate] = Field(default_factory=list, max_length=200)
    candidate_ids: list[str] = Field(default_factory=list, max_length=200)
    created_at: datetime
    expires_at: datetime
    trip_engine_version: str = Field(default=TRIP_ENGINE_VERSION, min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_candidate_ids(self) -> CandidateSet:
        if not _CANDIDATE_SET_ID_PATTERN.fullmatch(self.id):
            raise ValueError("candidate-set ID is not a valid opaque ID")
        actual_ids = [candidate.id for candidate in self.candidates]
        if self.candidate_ids != actual_ids:
            raise ValueError("candidate_ids must preserve the candidate-set order")
        if len(actual_ids) != len(set(actual_ids)):
            raise ValueError("candidate IDs in a set must be unique")
        if self.expires_at <= self.created_at:
            raise ValueError("candidate-set expiration must be after creation")
        return self


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _utc(parsed)


def _candidate_set_id() -> str:
    # The value is never used as a file name or SQL fragment.  It is still
    # restricted to a compact opaque alphabet for logs and API validation.
    return "cs_" + token_urlsafe(24).rstrip("=")


class CandidateSetStore:
    """SQLite-backed candidate-set repository with TTL and atomic writes."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        ttl_s: float = 3600.0,
        max_sets: int = 1000,
        trip_engine_version: str = TRIP_ENGINE_VERSION,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("candidate-set TTL must be positive")
        if max_sets < 1:
            raise ValueError("candidate-set max_sets must be positive")
        self.database_path = Path(database_path)
        self.ttl_s = float(ttl_s)
        self.max_sets = int(max_sets)
        self.trip_engine_version = trip_engine_version
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidate_sets (
                    candidate_set_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
                    schema_version TEXT NOT NULL,
                    trip_engine_version TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS candidate_set_candidates (
                    candidate_set_id TEXT NOT NULL
                        REFERENCES candidate_sets(candidate_set_id) ON DELETE CASCADE,
                    candidate_id TEXT NOT NULL,
                    position INTEGER NOT NULL CHECK (position >= 0),
                    candidate_json TEXT NOT NULL,
                    PRIMARY KEY (candidate_set_id, candidate_id),
                    UNIQUE (candidate_set_id, position)
                );

                CREATE INDEX IF NOT EXISTS idx_candidate_sets_expires_at
                    ON candidate_sets (expires_at);
                CREATE INDEX IF NOT EXISTS idx_candidate_set_candidates_set_position
                    ON candidate_set_candidates (candidate_set_id, position);
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _delete_expired(connection: sqlite3.Connection, now: datetime) -> int:
        cursor = connection.execute(
            "DELETE FROM candidate_sets WHERE expires_at <= ?",
            (_timestamp(now),),
        )
        return cursor.rowcount

    def save(
        self,
        response: TripCandidateResponse,
        *,
        now: datetime | None = None,
    ) -> CandidateSet:
        """Persist a complete response atomically and return its opaque ID."""

        materialized = [TripCandidate.model_validate(candidate) for candidate in response.candidates]
        reference_time = _utc(now)
        expires_at = reference_time + timedelta(seconds=self.ttl_s)
        candidate_set_id = _candidate_set_id()
        candidate_set = CandidateSet(
            id=candidate_set_id,
            request_fingerprint=response.request_fingerprint,
            candidates=materialized,
            candidate_ids=[candidate.id for candidate in materialized],
            created_at=reference_time,
            expires_at=expires_at,
            trip_engine_version=self.trip_engine_version,
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._delete_expired(connection, reference_time)
            active_count = connection.execute(
                "SELECT COUNT(*) AS count FROM candidate_sets"
            ).fetchone()["count"]
            if active_count >= self.max_sets:
                # Active sets are isolated user/tab state.  Never evict one
                # merely because a newer request arrived; only expired rows
                # are cleanup candidates.  A full store fails without
                # invalidating any active set.
                raise CandidateSetCapacityError(
                    "candidate-set capacity is full; active sets were preserved"
                )
            connection.execute(
                """
                INSERT INTO candidate_sets (
                    candidate_set_id, request_fingerprint, created_at, expires_at,
                    candidate_count, schema_version, trip_engine_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_set.id,
                    candidate_set.request_fingerprint,
                    _timestamp(candidate_set.created_at),
                    _timestamp(candidate_set.expires_at),
                    len(materialized),
                    CANDIDATE_SET_SCHEMA_VERSION,
                    candidate_set.trip_engine_version,
                ),
            )
            connection.executemany(
                """
                INSERT INTO candidate_set_candidates (
                    candidate_set_id, candidate_id, position, candidate_json
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        candidate_set.id,
                        candidate.id,
                        position,
                        json.dumps(
                            candidate.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    for position, candidate in enumerate(materialized)
                ],
            )
            connection.commit()
            return candidate_set
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """Delete only expired sets and return the number of metadata rows."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            deleted = self._delete_expired(connection, _utc(now))
            connection.commit()
            return deleted
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load(
        self,
        candidate_set_id: str,
        candidate_ids: list[str] | None = None,
        *,
        request_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> CandidateSet:
        """Load and validate one set with one batch query for candidate rows."""

        reference_time = _utc(now)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT candidate_set_id, request_fingerprint, created_at, expires_at,
                       candidate_count, schema_version, trip_engine_version
                FROM candidate_sets
                WHERE candidate_set_id = ?
                """,
                (candidate_set_id,),
            ).fetchone()
            if row is None:
                raise CandidateSetNotFoundError(
                    "요청한 후보 집합을 찾을 수 없습니다."
                )
            if row["schema_version"] != CANDIDATE_SET_SCHEMA_VERSION:
                raise CandidateSetVersionMismatchError(
                    "후보 집합 스키마 버전이 현재 서버와 호환되지 않습니다."
                )
            if row["trip_engine_version"] != self.trip_engine_version:
                raise CandidateSetVersionMismatchError(
                    "후보 집합이 현재 여행 엔진 버전과 호환되지 않습니다."
                )
            expires_at = _parse_timestamp(row["expires_at"])
            if expires_at <= reference_time:
                raise CandidateSetExpiredError(
                    "후보 집합의 보관 시간이 만료되었습니다. 다시 후보를 생성하세요."
                )
            stored_fingerprint = str(row["request_fingerprint"])
            if request_fingerprint is not None and request_fingerprint != stored_fingerprint:
                raise CandidateSetFingerprintMismatchError(
                    "후보 집합 fingerprint가 일치하지 않습니다."
                )

            rows = connection.execute(
                """
                SELECT candidate_id, position, candidate_json
                FROM candidate_set_candidates
                WHERE candidate_set_id = ?
                ORDER BY position ASC
                """,
                (candidate_set_id,),
            ).fetchall()
            if len(rows) != row["candidate_count"]:
                raise CandidateSetCorruptError(
                    "후보 집합 저장 행 수가 metadata와 일치하지 않습니다."
                )

            candidates_by_id: dict[str, TripCandidate] = {}
            ordered_ids: list[str] = []
            for candidate_row in rows:
                try:
                    decoded = json.loads(candidate_row["candidate_json"])
                    candidate = TripCandidate.model_validate(decoded)
                except Exception as error:  # Pydantic/JSON errors become a safe server error.
                    raise CandidateSetCorruptError(
                        "후보 집합의 저장된 candidate JSON이 유효하지 않습니다."
                    ) from error
                if candidate.id != candidate_row["candidate_id"] or candidate.id in candidates_by_id:
                    raise CandidateSetCorruptError(
                        "후보 집합의 candidate ID 무결성 검증에 실패했습니다."
                    )
                candidates_by_id[candidate.id] = candidate
                ordered_ids.append(candidate.id)

            selected_ids = ordered_ids if candidate_ids is None else list(candidate_ids)
            if len(selected_ids) != len(set(selected_ids)):
                raise CandidateSetMismatchError("candidate_ids는 중복될 수 없습니다.")
            missing = [candidate_id for candidate_id in selected_ids if candidate_id not in candidates_by_id]
            if missing:
                raise CandidateSetMismatchError(
                    "요청한 candidate가 해당 candidate_set_id에 속하지 않습니다."
                )
            selected_candidates = [candidates_by_id[candidate_id] for candidate_id in selected_ids]
            return CandidateSet(
                id=str(row["candidate_set_id"]),
                request_fingerprint=stored_fingerprint,
                candidates=selected_candidates,
                candidate_ids=selected_ids,
                created_at=_parse_timestamp(row["created_at"]),
                expires_at=expires_at,
                trip_engine_version=str(row["trip_engine_version"]),
            )
        finally:
            connection.close()

    def close(self) -> None:
        """Compatibility hook; this store intentionally keeps no connection open."""


__all__ = [
    "CANDIDATE_SET_SCHEMA_VERSION",
    "CandidateSet",
    "CandidateSetCapacityError",
    "CandidateSetCorruptError",
    "CandidateSetError",
    "CandidateSetExpiredError",
    "CandidateSetFingerprintMismatchError",
    "CandidateSetMismatchError",
    "CandidateSetNotFoundError",
    "CandidateSetStore",
    "CandidateSetVersionMismatchError",
]
