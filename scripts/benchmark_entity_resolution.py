"""Small reproducible V0.8 blocking benchmark.

Usage: ``python3 scripts/benchmark_entity_resolution.py --sizes 1000 10000``
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.entities.matcher import generate_candidate_pairs
from backend.entities.models import EntitySourceRecord
from backend.entities.repository import EntityResolutionRepository
from backend.entities.resolver import EntityResolver


def make_records(size: int) -> list[EntitySourceRecord]:
    fetched_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
    return [
        EntitySourceRecord(
            source="synthetic-a" if index % 2 == 0 else "synthetic-b",
            source_id=f"venue-{index}",
            source_url=f"https://synthetic.example/venue-{index}",
            name=f"Synthetic Venue {index}",
            category="restaurant" if index % 2 == 0 else "attraction",
            lat=35.0 + (index % 500) * 0.0001,
            lng=128.9 + (index // 500) * 0.0001,
            address=f"서울특별시 중구 Synthetic-ro {index}",
            rating=4.0,
            rating_scale=5.0,
            review_count=index + 1,
            fetched_at=fetched_at,
        )
        for index in range(size)
    ]


def benchmark(size: int) -> dict[str, float | int]:
    records = make_records(size)
    started = time.perf_counter()
    pairs = generate_candidate_pairs(records)
    candidate_ms = (time.perf_counter() - started) * 1_000.0

    started = time.perf_counter()
    result = EntityResolver().resolve(records, now=datetime(2026, 9, 15, tzinfo=timezone.utc))
    resolve_ms = (time.perf_counter() - started) * 1_000.0

    with tempfile.TemporaryDirectory(prefix="kto-entity-benchmark-") as directory:
        repository = EntityResolutionRepository(f"{directory}/entities.sqlite3")
        started = time.perf_counter()
        repository.persist(result)
        db_ms = (time.perf_counter() - started) * 1_000.0
    return {
        "records": size,
        "candidate_pairs": len(pairs),
        "canonical_count": result.stats.canonical_count,
        "candidate_ms": round(candidate_ms, 2),
        "resolve_ms": round(resolve_ms, 2),
        "db_ms": round(db_ms, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[1_000, 10_000])
    args = parser.parse_args()
    for size in args.sizes:
        print(benchmark(size))


if __name__ == "__main__":
    main()
