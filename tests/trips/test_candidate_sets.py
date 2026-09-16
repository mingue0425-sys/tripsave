from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from time import sleep

import pytest

from backend.trips.candidate_sets import (
    CandidateSetCapacityError,
    CandidateSetExpiredError,
    CandidateSetFingerprintMismatchError,
    CandidateSetMismatchError,
    CandidateSetNotFoundError,
    CandidateSetStore,
)
from backend.trips.service import TripCandidateService
from tests.trips.helpers import make_request

ANCHOR = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)


def test_candidate_set_is_persisted_and_loads_from_another_store_instance(tmp_path) -> None:
    response = TripCandidateService().assemble(make_request())
    first_store = CandidateSetStore(tmp_path / "sets.sqlite3", ttl_s=3600)
    second_store = CandidateSetStore(tmp_path / "sets.sqlite3", ttl_s=3600)

    saved = first_store.save(response, now=ANCHOR)
    loaded = second_store.load(
        saved.id,
        saved.candidate_ids,
        request_fingerprint=response.request_fingerprint,
        now=ANCHOR + timedelta(seconds=1),
    )

    assert saved.id.startswith("cs_")
    assert loaded.candidate_ids == saved.candidate_ids
    assert loaded.candidates[0].id == response.candidates[0].id


def test_concurrent_saves_create_independent_sets_without_overwrite(tmp_path) -> None:
    service = TripCandidateService()
    slow_response = service.assemble(make_request(start_date="2026-10-01", end_date="2026-10-03"))
    fast_response = service.assemble(make_request(start_date="2026-11-01", end_date="2026-11-03"))
    store = CandidateSetStore(tmp_path / "sets.sqlite3", ttl_s=3600, max_sets=10)
    barrier = Barrier(2)

    def save_after_start(response, delay):
        barrier.wait()
        if delay:
            sleep(delay)
        return store.save(response, now=ANCHOR)

    with ThreadPoolExecutor(max_workers=2) as executor:
        # The fast request commits first and the slow request commits second.
        # Both sets must remain addressable; there is no process-wide "latest" slot.
        first, second = executor.map(
            lambda args: save_after_start(*args),
            ((slow_response, 0.05), (fast_response, 0)),
        )

    assert first.id != second.id
    assert first.request_fingerprint != second.request_fingerprint
    assert store.load(first.id, now=ANCHOR + timedelta(seconds=1)).candidate_ids
    assert store.load(second.id, now=ANCHOR + timedelta(seconds=1)).candidate_ids


def test_wrong_set_candidate_and_fingerprint_are_rejected(tmp_path) -> None:
    service = TripCandidateService()
    store = CandidateSetStore(tmp_path / "sets.sqlite3", ttl_s=3600, max_sets=10)
    first = store.save(service.assemble(make_request()), now=ANCHOR)
    second = store.save(
        service.assemble(make_request(start_date="2026-11-01", end_date="2026-11-03")),
        now=ANCHOR,
    )

    with pytest.raises(CandidateSetMismatchError):
        store.load(first.id, second.candidate_ids, now=ANCHOR + timedelta(seconds=1))
    with pytest.raises(CandidateSetFingerprintMismatchError):
        store.load(
            first.id,
            request_fingerprint="0" * 64,
            now=ANCHOR + timedelta(seconds=1),
        )


def test_expired_sets_are_distinguished_and_cleanup_does_not_touch_active_sets(tmp_path) -> None:
    store = CandidateSetStore(tmp_path / "sets.sqlite3", ttl_s=60, max_sets=10)
    response = TripCandidateService().assemble(make_request())
    saved = store.save(response, now=ANCHOR)

    with pytest.raises(CandidateSetExpiredError):
        store.load(saved.id, now=ANCHOR + timedelta(seconds=61))

    assert store.cleanup_expired(now=ANCHOR + timedelta(seconds=61)) == 1
    with pytest.raises(CandidateSetNotFoundError):
        # After lazy cleanup the set is no longer present and cannot return
        # candidate data.
        store.load(saved.id, now=ANCHOR + timedelta(seconds=62))


def test_capacity_failure_preserves_all_active_sets(tmp_path) -> None:
    store = CandidateSetStore(tmp_path / "sets.sqlite3", ttl_s=3600, max_sets=2)
    service = TripCandidateService()
    first = store.save(service.assemble(make_request()), now=ANCHOR)
    second = store.save(
        service.assemble(make_request(start_date="2026-11-01", end_date="2026-11-03")),
        now=ANCHOR,
    )

    with pytest.raises(CandidateSetCapacityError):
        store.save(
            service.assemble(make_request(start_date="2026-12-01", end_date="2026-12-03")),
            now=ANCHOR,
        )

    assert store.load(first.id, now=ANCHOR + timedelta(seconds=1)).candidate_ids
    assert store.load(second.id, now=ANCHOR + timedelta(seconds=1)).candidate_ids
