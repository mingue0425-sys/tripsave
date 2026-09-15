"""Deterministic, precision-first source record resolution."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from pydantic import ValidationError

from backend.accommodation.models import AccommodationOffer
from backend.entities.matcher import (
    PairMatch,
    coerce_source_record,
    compare_records,
    generate_candidate_pairs,
    source_record_fingerprint,
    source_record_id,
)
from backend.entities.models import (
    MATCHER_VERSION,
    CanonicalPlace,
    EntityResolveResponse,
    EntityResolverStats,
    EntitySourceRecord,
    MatchDecision,
    MatchTrace,
    QuarantinedRecord,
    SourceMembership,
)
from backend.entities.normalize import has_hangul, normalize_address, normalize_text
from backend.entities.rating import (
    build_rating_profile,
    category_rating_context,
    source_confidence,
)

COORDINATE_CONFLICT_METERS = 300.0
LOGGER = logging.getLogger(__name__)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _pair_key(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first < second else (second, first)


def _source_key(value: str) -> str:
    return normalize_text(value)


def _timestamp_sort_key(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record_completeness(record: EntitySourceRecord) -> int:
    return sum(
        (
            bool(record.source_id),
            bool(record.source_url),
            bool(record.address),
            record.lat is not None and record.lng is not None,
            record.rating is not None and record.rating_scale is not None,
            record.review_count is not None,
        )
    )


def _representative_name(records: list[tuple[str, EntitySourceRecord]]) -> str:
    chosen = min(
        records,
        key=lambda item: (
            not has_hangul(item[1].name),
            -_record_completeness(item[1]),
            len(normalize_text(item[1].name)),
            item[0],
        ),
    )
    return chosen[1].name.strip()


def _representative_address(records: list[tuple[str, EntitySourceRecord]]) -> str | None:
    with_address = [item for item in records if item[1].address and item[1].address.strip()]
    if not with_address:
        return None
    chosen = min(
        with_address,
        key=lambda item: (
            not has_hangul(item[1].address),
            len(normalize_address(item[1].address)),
            item[0],
        ),
    )
    return chosen[1].address.strip() if chosen[1].address else None


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


def _canonical_coordinates(
    records: list[tuple[str, EntitySourceRecord]],
) -> tuple[float | None, float | None, bool]:
    coordinates = [
        (record.lat, record.lng)
        for _, record in records
        if record.lat is not None and record.lng is not None
    ]
    if not coordinates:
        return None, None, False
    spread = max(
        (_haversine_m(first[0], first[1], second[0], second[1]) for first, second in combinations(coordinates, 2)),
        default=0.0,
    )
    if spread > COORDINATE_CONFLICT_METERS:
        # A large disagreement must not become a fake midpoint on the map.
        return None, None, True
    latitudes = sorted(value[0] for value in coordinates)
    longitudes = sorted(value[1] for value in coordinates)
    middle = len(coordinates) // 2
    if len(coordinates) % 2:
        lat, lng = latitudes[middle], longitudes[middle]
    else:
        lat = (latitudes[middle - 1] + latitudes[middle]) / 2.0
        lng = (longitudes[middle - 1] + longitudes[middle]) / 2.0
    return lat, lng, False


def _raw_fields(records: list[tuple[str, EntitySourceRecord]]) -> dict[str, list[Any]]:
    common_fields = {
        "source",
        "source_id",
        "source_url",
        "name",
        "category",
        "lat",
        "lng",
        "address",
        "rating",
        "rating_scale",
        "review_count",
        "fetched_at",
    }
    result: dict[str, list[Any]] = defaultdict(list)
    for _, record in records:
        payload = record.model_dump(mode="json", exclude_none=True)
        for key, value in payload.items():
            if key in common_fields or value in (None, "", [], {}):
                continue
            if value not in result[key]:
                result[key].append(value)
    addresses = sorted({record.address.strip() for _, record in records if record.address and record.address.strip()})
    if addresses:
        result["source_addresses"] = addresses
    coordinates = sorted(
        {
            (record.lat, record.lng)
            for _, record in records
            if record.lat is not None and record.lng is not None
        }
    )
    if coordinates:
        result["source_coordinates"] = [list(value) for value in coordinates]
    return {key: values for key, values in sorted(result.items())}


class EntityResolver:
    """Resolve records with deterministic ordering and conservative clusters."""

    def __init__(self, *, overrides: Mapping[tuple[str, str], MatchDecision] | None = None) -> None:
        self.overrides = {
            _pair_key(first, second): decision
            for (first, second), decision in (overrides or {}).items()
        }

    def _compare(
        self,
        first: EntitySourceRecord,
        second: EntitySourceRecord,
        cache: dict[tuple[str, str], PairMatch],
    ) -> PairMatch:
        first_id, second_id = source_record_id(first), source_record_id(second)
        key = _pair_key(first_id, second_id)
        if key not in cache:
            cache[key] = compare_records(first, second, override=self.overrides.get(key))
        return cache[key]

    def _prepare_records(
        self,
        records: list[Any],
    ) -> tuple[list[tuple[str, EntitySourceRecord]], list[QuarantinedRecord]]:
        valid: dict[str, tuple[EntitySourceRecord, str]] = {}
        quarantined: list[QuarantinedRecord] = []
        for index, raw_record in enumerate(records):
            try:
                record = coerce_source_record(raw_record)
                record_id = source_record_id(record)
                fingerprint = source_record_fingerprint(record)
            except (ValidationError, TypeError, ValueError) as error:
                quarantined.append(
                    QuarantinedRecord(
                        input_index=index,
                        reason=f"{type(error).__name__}: {str(error)[:450]}",
                    )
                )
                continue
            previous = valid.get(record_id)
            if previous is None:
                valid[record_id] = (record, fingerprint)
            elif previous[1] != fingerprint:
                # A source-native identity is the strongest signal.  A
                # refreshed row can legitimately change rating, address
                # formatting, or fetched_at; keep the newest snapshot and let
                # the source-owned raw table retain the older payload.
                previous_record = previous[0]
                previous_time = previous_record.fetched_at
                current_time = record.fetched_at
                if (_timestamp_sort_key(current_time), fingerprint) > (
                    _timestamp_sort_key(previous_time),
                    previous[1],
                ):
                    valid[record_id] = (record, fingerprint)
            # Byte-for-byte duplicate source rows are one source record, not a
            # reason to create a second canonical place.
        return sorted(((record_id, value[0]) for record_id, value in valid.items()), key=lambda item: item[0]), quarantined

    def _build_canonical(
        self,
        members: list[tuple[str, EntitySourceRecord]],
        *,
        pair_matches: dict[tuple[str, str], PairMatch],
        now: datetime,
        rating_priors: dict[str, float],
        rating_minimums: dict[str, int],
    ) -> CanonicalPlace:
        members = sorted(members, key=lambda item: item[0])
        canonical_id = "cp_" + hashlib.sha256(members[0][0].encode("utf-8")).hexdigest()[:32]
        lat, lng, coordinate_conflict = _canonical_coordinates(members)
        record_ids = {index: record_id for index, (record_id, _) in enumerate(members)}
        rating = build_rating_profile(
            [record for _, record in members],
            source_record_ids=record_ids,
            priors=rating_priors,
            minimums=rating_minimums,
        )
        record_confidences = [source_confidence(record, now=now) for _, record in members]

        matched_scores: list[float] = []
        traces: list[MatchTrace] = []
        anchor_id, anchor = members[0]
        for (first_id, first_record), (second_id, second_record) in combinations(members, 2):
            match = self._compare(first_record, second_record, pair_matches)
            matched_scores.append(match.score)
            traces.append(
                MatchTrace(
                    record_a=match.record_a,
                    record_b=match.record_b,
                    score=match.score,
                    decision=match.decision,
                    method=match.method,
                    reason=match.reason,
                    name_score=match.signals.name_score,
                    address_score=match.signals.address_score,
                    coordinate_score=match.signals.coordinate_score,
                    category_score=match.signals.category_score,
                    source_identity_score=match.signals.source_identity_score,
                    distance_m=match.signals.distance_m,
                    created_at=now,
                )
            )
        cluster_agreement = sum(matched_scores) / len(matched_scores) if matched_scores else 1.0
        confidence = _clamp(sum(record_confidences) / len(record_confidences) * (0.75 + 0.25 * cluster_agreement))
        if coordinate_conflict:
            confidence = _clamp(confidence * 0.75)
        if rating.rating_disagreement:
            confidence = _clamp(confidence * 0.90)

        memberships: list[SourceMembership] = []
        for index, (record_id, record) in enumerate(members):
            if index == 0:
                match_score = 1.0
                match_method = "SINGLE_SOURCE_CANONICAL" if len(members) == 1 else "CLUSTER_ANCHOR"
            else:
                match = pair_matches[_pair_key(anchor_id, record_id)]
                match_score = match.score
                match_method = match.method
            memberships.append(
                SourceMembership(
                    source=record.source,
                    source_id=record.source_id,
                    source_url=record.source_url,
                    source_record_id=record_id,
                    source_record_fingerprint=source_record_fingerprint(record),
                    match_score=match_score,
                    match_method=match_method,
                    source_confidence=source_confidence(record, now=now),
                    merged_at=now,
                )
            )

        aliases = sorted({record.name.strip() for _, record in members if record.name.strip()}, key=lambda value: (normalize_text(value), value))
        return CanonicalPlace(
            id=canonical_id,
            name=_representative_name(members),
            category=normalize_text(anchor.category),
            lat=lat,
            lng=lng,
            address=_representative_address(members),
            normalized_rating=rating.normalized_rating,
            rating_confidence=rating.rating_confidence,
            review_count_total=rating.observed_review_count_sum,
            observed_review_count_sum=rating.observed_review_count_sum,
            source_count=len({_source_key(record.source) for _, record in members}),
            confidence=confidence,
            sources=memberships,
            aliases=aliases,
            raw_fields=_raw_fields(members),
            coordinate_conflict=coordinate_conflict,
            rating_disagreement=rating.rating_disagreement,
            decision_trace=traces,
            matcher_version=MATCHER_VERSION,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _all_match(
        record: EntitySourceRecord,
        cluster: list[tuple[str, EntitySourceRecord]],
        *,
        compare,
        cache: dict[tuple[str, str], PairMatch],
    ) -> bool:
        return all(compare(record, member, cache).decision == MatchDecision.MATCH for _, member in cluster)

    def resolve(
        self,
        records: Iterable[Any],
        *,
        offers: Iterable[AccommodationOffer | Mapping[str, Any]] = (),
        now: datetime | None = None,
    ) -> EntityResolveResponse:
        """Resolve supplied raw records; no external source is fetched here."""

        materialized_records = list(records)
        reference_time = now or datetime.now(timezone.utc)
        valid_records, quarantined = self._prepare_records(materialized_records)
        record_by_id = dict(valid_records)
        rating_priors, rating_minimums = category_rating_context(
            [record for _, record in valid_records]
        )
        blocked_pairs = generate_candidate_pairs([record for _, record in valid_records])
        # A persisted manual override is itself a blocking key.  This keeps a
        # deliberately verified alias effective even when its names share no
        # lexical/spatial block.
        blocked_pair_set = set(blocked_pairs)
        for first_id, second_id in self.overrides:
            key = _pair_key(first_id, second_id)
            if first_id in record_by_id and second_id in record_by_id and key not in blocked_pair_set:
                blocked_pairs.append(key)
                blocked_pair_set.add(key)
        blocked_pairs.sort()
        pair_matches: dict[tuple[str, str], PairMatch] = {}
        candidate_neighbors: dict[str, set[str]] = defaultdict(set)
        for first_id, second_id in blocked_pairs:
            candidate_neighbors[first_id].add(second_id)
            candidate_neighbors[second_id].add(first_id)
            pair_matches[(first_id, second_id)] = compare_records(
                record_by_id[first_id],
                record_by_id[second_id],
                override=self.overrides.get((first_id, second_id)),
            )

        # Sort by stable IDs, then add a record only if it matches every member
        # of a cluster.  This is intentionally stricter than union-find: an
        # A-B and B-C edge cannot force A-C into the same entity.
        clusters: list[list[tuple[str, EntitySourceRecord]]] = []
        cluster_by_record: dict[str, int] = {}
        for record_id, record in valid_records:
            candidate_cluster_indexes = {
                cluster_by_record[neighbor_id]
                for neighbor_id in candidate_neighbors.get(record_id, ())
                if neighbor_id in cluster_by_record
                and pair_matches[_pair_key(record_id, neighbor_id)].decision == MatchDecision.MATCH
            }
            compatible: list[int] = []
            for cluster_index in sorted(candidate_cluster_indexes):
                cluster = clusters[cluster_index]
                if self._all_match(record, cluster, compare=self._compare, cache=pair_matches):
                    compatible.append(cluster_index)
            if not compatible:
                clusters.append([(record_id, record)])
                cluster_by_record[record_id] = len(clusters) - 1
                continue
            if len(compatible) == 1:
                clusters[compatible[0]].append((record_id, record))
                cluster_by_record[record_id] = compatible[0]
                continue
            # Merge compatible clusters only after checking every cross-pair;
            # otherwise keep the new record separate rather than choosing a
            # potentially false transitive link.
            combined = [item for index in compatible for item in clusters[index]] + [(record_id, record)]
            if all(
                self._compare(first_record, second_record, pair_matches).decision == MatchDecision.MATCH
                for (first_id, first_record), (second_id, second_record) in combinations(combined, 2)
            ):
                first_index = compatible[0]
                clusters[first_index] = sorted(combined, key=lambda item: item[0])
                for index in reversed(compatible[1:]):
                    clusters.pop(index)
                for member_id, _member in clusters[first_index]:
                    cluster_by_record[member_id] = first_index
                # Removing earlier/later list entries shifts the indices of
                # clusters after them.  Re-indexing is cheap because only the
                # rare multi-cluster compatibility path reaches this branch.
                cluster_by_record = {
                    member_id: cluster_index
                    for cluster_index, cluster in enumerate(clusters)
                    for member_id, _member in cluster
                }
            else:
                clusters.append([(record_id, record)])
                cluster_by_record[record_id] = len(clusters) - 1

        canonical_places = [
            self._build_canonical(
                cluster,
                pair_matches=pair_matches,
                now=reference_time,
                rating_priors=rating_priors,
                rating_minimums=rating_minimums,
            )
            for cluster in sorted(clusters, key=lambda value: min(item[0] for item in value))
        ]
        canonical_by_record = {
            record_id: canonical.id
            for cluster, canonical in zip(
                sorted(clusters, key=lambda value: min(item[0] for item in value)),
                canonical_places,
            )
            for record_id, _ in cluster
        }

        valid_offers: list[AccommodationOffer] = []
        for raw_offer in offers:
            try:
                valid_offers.append(
                    raw_offer if isinstance(raw_offer, AccommodationOffer) else AccommodationOffer.model_validate(raw_offer)
                )
            except (ValidationError, TypeError, ValueError):
                # An invalid offer is not a place and must not create a
                # synthetic accommodation entity.  It is exposed as unlinked.
                continue
        offers_by_canonical: dict[str, list[AccommodationOffer]] = defaultdict(list)
        unlinked_offers: list[AccommodationOffer] = []
        for offer in valid_offers:
            matching_ids = [
                record_id
                for record_id, record in valid_records
                if normalize_text(record.category) == "accommodation"
                and _source_key(record.source) == _source_key(offer.source)
                and (
                    (offer.place_source_id is not None and record.source_id == offer.place_source_id)
                    or (offer.place_source_id is None)
                )
            ]
            if offer.place_source_id is None and len(matching_ids) != 1:
                matching_ids = []
            if len(matching_ids) == 1 and matching_ids[0] in canonical_by_record:
                offers_by_canonical[canonical_by_record[matching_ids[0]]].append(offer)
            else:
                unlinked_offers.append(offer)

        candidates = [
            match.as_candidate(created_at=reference_time)
            for _, match in sorted(pair_matches.items(), key=lambda item: item[0])
        ]
        matched_count = sum(candidate.decision == MatchDecision.MATCH for candidate in candidates)
        ambiguous_count = sum(candidate.decision == MatchDecision.AMBIGUOUS for candidate in candidates)
        rejected_count = sum(candidate.decision == MatchDecision.NO_MATCH for candidate in candidates)
        multi_source_count = sum(place.source_count > 1 for place in canonical_places)
        stats = EntityResolverStats(
            input_records=len(materialized_records),
            valid_records=len(valid_records),
            quarantined_records=len(quarantined),
            candidate_pairs=len(candidates),
            matched_pairs=matched_count,
            ambiguous_pairs=ambiguous_count,
            rejected_pairs=rejected_count,
            canonical_count=len(canonical_places),
            multi_source_count=multi_source_count,
            single_source_count=len(canonical_places) - multi_source_count,
        )
        LOGGER.debug(
            "entity resolution records=%d candidates=%d matches=%d ambiguous=%d rejected=%d "
            "canonical=%d multi_source=%d quarantined=%d",
            stats.valid_records,
            stats.candidate_pairs,
            stats.matched_pairs,
            stats.ambiguous_pairs,
            stats.rejected_pairs,
            stats.canonical_count,
            stats.multi_source_count,
            stats.quarantined_records,
        )
        return EntityResolveResponse(
            canonical_places=canonical_places,
            candidates=candidates,
            source_records={record_id: record for record_id, record in valid_records},
            offers_by_canonical_id={key: value for key, value in sorted(offers_by_canonical.items())},
            unlinked_offers=unlinked_offers,
            quarantined=quarantined,
            stats=stats,
        )


__all__ = ["COORDINATE_CONFLICT_METERS", "EntityResolver"]
