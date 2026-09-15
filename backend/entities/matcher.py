"""Conservative candidate blocking and pairwise place matching."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any

from backend.entities.models import (
    MATCHER_VERSION,
    EntityMatchCandidate,
    EntitySourceRecord,
    MatchDecision,
)
from backend.entities.normalize import (
    address_tokens,
    branch_location_tokens,
    chain_tokens,
    coordinate_cell,
    meaningful_name_tokens,
    name_tokens,
    normalize_address,
    normalize_name,
    normalize_text,
    region_keys,
)
from backend.place_models import PlaceSourceRecord

MAX_BLOCK_SIZE = 250
DISTANCE_REJECT_METERS = 2_000.0
STRONG_DISTANCE_METERS = 300.0


@dataclass(frozen=True)
class MatchSignals:
    name_score: float
    address_score: float | None
    coordinate_score: float | None
    category_score: float
    source_identity_score: float
    distance_m: float | None


@dataclass(frozen=True)
class PairMatch:
    record_a: str
    record_b: str
    score: float
    decision: MatchDecision
    method: str
    reason: str
    signals: MatchSignals

    def as_candidate(self, *, created_at: datetime) -> EntityMatchCandidate:
        return EntityMatchCandidate(
            record_a=self.record_a,
            record_b=self.record_b,
            score=self.score,
            decision=self.decision,
            reason=self.reason,
            matcher_version=MATCHER_VERSION,
            created_at=created_at,
            name_score=self.signals.name_score,
            address_score=self.signals.address_score,
            coordinate_score=self.signals.coordinate_score,
            category_score=self.signals.category_score,
            source_identity_score=self.signals.source_identity_score,
            distance_m=self.signals.distance_m,
            method=self.method,
        )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def coerce_source_record(value: EntitySourceRecord | PlaceSourceRecord | Mapping[str, Any]) -> EntitySourceRecord:
    """Accept V0.6/V0.7 models without making either model depend on V0.8."""

    if isinstance(value, EntitySourceRecord):
        return value
    if isinstance(value, PlaceSourceRecord):
        return EntitySourceRecord.model_validate(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return EntitySourceRecord.model_validate(dict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return EntitySourceRecord.model_validate(model_dump(mode="python"))
    raise TypeError("source record must be a PlaceSourceRecord or mapping")


def source_record_fingerprint(record: EntitySourceRecord) -> str:
    """Hash all validated fields, including source-native extras."""

    payload = record.model_dump(mode="json", exclude_none=False)
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def source_record_id(record: EntitySourceRecord) -> str:
    """Create an ID stable across rebuilds and raw input ordering.

    A source-native ID is preferred.  The fallback identity deliberately uses
    durable metadata rather than rating/fetched time so a refresh keeps the
    same canonical anchor.
    """

    source = normalize_text(record.source)
    category = normalize_text(record.category)
    if record.source_id:
        identity = f"id|{source}|{category}|{normalize_text(record.source_id)}"
    else:
        lat_lng = ""
        if record.lat is not None and record.lng is not None:
            lat_lng = f"{record.lat:.6f}|{record.lng:.6f}"
        identity = "|".join(
            (
                "fallback",
                source,
                category,
                normalize_name(record.name),
                normalize_address(record.address),
                lat_lng,
                normalize_text(record.source_url),
            )
        )
    return "sr_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _source_identity(record: EntitySourceRecord) -> tuple[str, str] | None:
    if not record.source_id:
        return None
    return normalize_text(record.source), normalize_text(record.source_id)


def _haversine_m(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    radius_m = 6_371_008.8
    lat1, lat2 = math.radians(lat_a), math.radians(lat_b)
    d_lat = lat2 - lat1
    d_lng = math.radians(lng_b - lng_a)
    value = (
        math.sin(d_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(d_lng / 2.0) ** 2
    )
    return radius_m * 2.0 * math.asin(math.sqrt(max(0.0, min(1.0, value))))


def _token_similarity(tokens_a: tuple[str, ...], tokens_b: tuple[str, ...]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    set_a, set_b = set(tokens_a), set(tokens_b)
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    jaccard = intersection / union if union else 0.0
    subset = intersection / min(len(set_a), len(set_b))
    return _clamp(max(jaccard, subset * 0.96))


def _text_similarity(value_a: str, value_b: str) -> float:
    if not value_a or not value_b:
        return 0.0
    if value_a == value_b:
        return 1.0
    return SequenceMatcher(None, value_a, value_b, autojunk=False).ratio()


def name_similarity(value_a: str | None, value_b: str | None) -> float:
    normalized_a = normalize_name(value_a)
    normalized_b = normalize_name(value_b)
    if not normalized_a or not normalized_b:
        return 0.0
    token_score = _token_similarity(name_tokens(value_a), name_tokens(value_b))
    text_score = _text_similarity(normalized_a, normalized_b)
    return _clamp(max(token_score, text_score))


def address_similarity(value_a: str | None, value_b: str | None) -> float | None:
    normalized_a = normalize_address(value_a)
    normalized_b = normalize_address(value_b)
    if not normalized_a or not normalized_b:
        return None
    token_score = _token_similarity(address_tokens(value_a), address_tokens(value_b))
    text_score = _text_similarity(normalized_a, normalized_b)
    return _clamp(max(token_score, text_score))


def _coordinate_similarity(distance_m: float | None) -> float | None:
    if distance_m is None:
        return None
    if distance_m <= 30.0:
        return 1.0
    if distance_m <= 100.0:
        return 0.90
    if distance_m <= STRONG_DISTANCE_METERS:
        return 0.75
    return _clamp(1.0 - distance_m / DISTANCE_REJECT_METERS)


def _distance_for(a: EntitySourceRecord, b: EntitySourceRecord) -> float | None:
    if None in (a.lat, a.lng, b.lat, b.lng):
        return None
    return _haversine_m(a.lat, a.lng, b.lat, b.lng)


def _weighted_score(signals: MatchSignals) -> float:
    # Category is a hard constraint, not a soft boost.  Missing address or
    # coordinates remove their weight rather than turning unknown into zero.
    components: list[tuple[float, float]] = [(signals.name_score, 0.50)]
    if signals.address_score is not None:
        components.append((signals.address_score, 0.25))
    if signals.coordinate_score is not None:
        components.append((signals.coordinate_score, 0.25))
    denominator = sum(weight for _, weight in components)
    return _clamp(sum(value * weight for value, weight in components) / denominator)


def _pair_ids(a: EntitySourceRecord, b: EntitySourceRecord) -> tuple[str, str]:
    first, second = source_record_id(a), source_record_id(b)
    return (first, second) if first < second else (second, first)


def _signals(a: EntitySourceRecord, b: EntitySourceRecord) -> MatchSignals:
    distance_m = _distance_for(a, b)
    return MatchSignals(
        name_score=name_similarity(a.name, b.name),
        address_score=address_similarity(a.address, b.address),
        coordinate_score=_coordinate_similarity(distance_m),
        category_score=1.0 if normalize_text(a.category) == normalize_text(b.category) else 0.0,
        source_identity_score=(
            1.0
            if _source_identity(a) is not None and _source_identity(a) == _source_identity(b)
            else 0.0
        ),
        distance_m=distance_m,
    )


def compare_records(
    a: EntitySourceRecord,
    b: EntitySourceRecord,
    *,
    override: MatchDecision | None = None,
) -> PairMatch:
    """Compare two records with hard safety gates before thresholds.

    A high name score alone is never a MATCH.  Address and coordinates are
    corroborating signals, while exact source identity is the only identity
    signal that can stand alone.
    """

    record_a, record_b = _pair_ids(a, b)
    signals = _signals(a, b)
    if override is not None:
        method = "MANUAL_VERIFIED_ALIAS" if override == MatchDecision.MATCH else "MANUAL_OVERRIDE"
        return PairMatch(
            record_a=record_a,
            record_b=record_b,
            score=1.0 if override == MatchDecision.MATCH else 0.0,
            decision=override,
            method=method,
            reason="manual override",
            signals=signals,
        )

    category_a, category_b = normalize_text(a.category), normalize_text(b.category)
    if category_a != category_b:
        return PairMatch(record_a, record_b, 0.0, MatchDecision.NO_MATCH, "CATEGORY_CONFLICT", "category conflict", signals)

    regions_a = region_keys(a.name, a.address)
    regions_b = region_keys(b.name, b.address)
    if regions_a and regions_b and regions_a.isdisjoint(regions_b):
        return PairMatch(record_a, record_b, 0.0, MatchDecision.NO_MATCH, "REGION_CONFLICT", "explicit region conflict", signals)

    if signals.distance_m is not None and signals.distance_m > DISTANCE_REJECT_METERS:
        return PairMatch(record_a, record_b, 0.0, MatchDecision.NO_MATCH, "DISTANCE_CONFLICT", "coordinates are more than 2km apart", signals)

    identity_a, identity_b = _source_identity(a), _source_identity(b)
    if identity_a is not None and identity_a == identity_b:
        return PairMatch(record_a, record_b, 1.0, MatchDecision.MATCH, "EXACT_SOURCE_ID", "same source-native identity", signals)
    if identity_a is not None and identity_b is not None and identity_a[0] == identity_b[0]:
        return PairMatch(record_a, record_b, 0.0, MatchDecision.NO_MATCH, "SOURCE_ID_CONFLICT", "distinct IDs from one source", signals)

    chains_a = chain_tokens(a.name)
    chains_b = chain_tokens(b.name)
    branches_a = branch_location_tokens(a.name)
    branches_b = branch_location_tokens(b.name)
    if chains_a & chains_b and branches_a and branches_b and branches_a.isdisjoint(branches_b):
        return PairMatch(record_a, record_b, 0.0, MatchDecision.NO_MATCH, "CHAIN_BRANCH_CONFLICT", "chain branch/location conflict", signals)

    secondary_evidence = (
        (signals.address_score is not None and signals.address_score >= 0.80)
        or (signals.distance_m is not None and signals.distance_m <= STRONG_DISTANCE_METERS)
    )
    if signals.name_score < 0.78:
        return PairMatch(record_a, record_b, _weighted_score(signals), MatchDecision.NO_MATCH, "INSUFFICIENT_NAME", "name evidence is too weak", signals)
    if not secondary_evidence:
        decision = MatchDecision.AMBIGUOUS if signals.name_score >= 0.75 else MatchDecision.NO_MATCH
        return PairMatch(record_a, record_b, signals.name_score, decision, "NAME_ONLY", "name similarity has no corroborating signal", signals)

    # Exact address or exact coordinate without a compatible name is not an
    # entity match: a complex can host many restaurants and attractions.
    if signals.name_score < 0.86:
        return PairMatch(record_a, record_b, _weighted_score(signals), MatchDecision.NO_MATCH, "SHARED_LOCATION_ONLY", "location agrees but name does not", signals)

    score = _weighted_score(signals)
    # A close coordinate and a very strong multilingual name match can be
    # decisive even when two sources format road addresses differently.  This
    # still requires both independent signals; name-only never reaches here.
    if signals.name_score >= 0.94 and signals.coordinate_score is not None and signals.coordinate_score >= 0.90:
        score = max(score, 0.95)
    if signals.name_score >= 0.94 and signals.address_score is not None and signals.address_score >= 0.84:
        score = max(score, 0.94)
    if score >= 0.92:
        if signals.address_score is not None and signals.coordinate_score is not None:
            method = "NAME_ADDRESS_COORDINATE_MATCH"
        elif signals.address_score is not None:
            method = "EXACT_NAME_ADDRESS" if signals.name_score >= 0.96 else "NAME_ADDRESS_MATCH"
        else:
            method = "NAME_COORDINATE_MATCH"
        return PairMatch(record_a, record_b, score, MatchDecision.MATCH, method, "multiple independent signals agree", signals)
    if score >= 0.75:
        return PairMatch(record_a, record_b, score, MatchDecision.AMBIGUOUS, "MULTI_SIGNAL_AMBIGUOUS", "signals are compatible but below the merge threshold", signals)
    return PairMatch(record_a, record_b, score, MatchDecision.NO_MATCH, "LOW_COMBINED_SCORE", "combined evidence is below the ambiguity threshold", signals)


def _block_keys(record: EntitySourceRecord) -> tuple[str, ...]:
    category = normalize_text(record.category)
    name = meaningful_name_tokens(record.name)
    normalized_name = normalize_name(record.name)
    region = ",".join(sorted(region_keys(record.name, record.address))) or "unknown"
    keys: set[str] = set()

    prefix = " ".join(name[:3])
    tail = " ".join(name[-2:])
    has_numeric_token = any(token.isdigit() for token in name)
    if normalized_name and len(name) >= 2:
        keys.add(f"{category}|name|{normalized_name}")
        keys.add(f"{category}|name-set|{' '.join(sorted(set(name)))}")
        # Three tokens retain branch/location/number evidence.  A two-token
        # prefix made every ``Synthetic Venue <n>`` row share one huge bucket.
        keys.add(f"{category}|region-name|{region}|{prefix}")
        if not has_numeric_token:
            keys.add(f"{category}|region-name-tail|{region}|{tail}")
    if record.source_id:
        keys.add(f"{category}|source-id|{normalize_text(record.source)}|{normalize_text(record.source_id)}")
    if record.lat is not None and record.lng is not None and len(name) >= 2:
        cell_lat, cell_lng = coordinate_cell(record.lat, record.lng)
        # Neighbour cells cover the 30--300m evidence bands without making a
        # city-wide spatial block.
        for delta_lat in (-1, 0, 1):
            for delta_lng in (-1, 0, 1):
                keys.add(f"{category}|cell-name|{cell_lat + delta_lat}|{cell_lng + delta_lng}|{prefix}")
                if not has_numeric_token:
                    keys.add(f"{category}|cell-name-tail|{cell_lat + delta_lat}|{cell_lng + delta_lng}|{tail}")
    address = address_tokens(record.address)
    if address and len(name) >= 2:
        address_tail = " ".join(address[-3:])
        keys.add(f"{category}|address-name|{address_tail}|{prefix}")
    return tuple(sorted(keys))


def generate_candidate_pairs(records: list[EntitySourceRecord]) -> list[tuple[str, str]]:
    """Generate bounded, deterministic pairs instead of a global O(n²) pass."""

    buckets: dict[str, list[str]] = {}
    for record in sorted(records, key=source_record_id):
        record_id = source_record_id(record)
        for key in _block_keys(record):
            bucket = buckets.setdefault(key, [])
            if len(bucket) < MAX_BLOCK_SIZE:
                bucket.append(record_id)

    pairs: set[tuple[str, str]] = set()
    for bucket in buckets.values():
        for first, second in combinations(sorted(set(bucket)), 2):
            pairs.add((first, second))
    return sorted(pairs)


__all__ = [
    "DISTANCE_REJECT_METERS",
    "MAX_BLOCK_SIZE",
    "STRONG_DISTANCE_METERS",
    "MatchSignals",
    "PairMatch",
    "address_similarity",
    "coerce_source_record",
    "compare_records",
    "generate_candidate_pairs",
    "name_similarity",
    "source_record_fingerprint",
    "source_record_id",
]
