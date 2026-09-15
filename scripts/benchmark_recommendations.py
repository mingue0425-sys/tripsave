"""Reproducible V1.0 ranking benchmark.

Usage: ``python3 scripts/benchmark_recommendations.py --sizes 50 200``
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.recommendations import RecommendationMode, RecommendationService
from backend.trips.models import TripCandidate
from backend.trips.service import TripCandidateService
from tests.trips.helpers import make_offer, make_place, make_request

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def make_candidates(size: int) -> list[TripCandidate]:
    base = TripCandidateService().assemble(
        make_request(
            canonical_places=[
                make_place(
                    "benchmark-hotel",
                    "Benchmark Hotel",
                    "accommodation",
                    source="booking",
                    source_id="benchmark-hotel",
                )
            ],
            offers=[
                make_offer(
                    source_id="benchmark-hotel",
                    source_offer_id="benchmark-offer",
                )
            ],
        ),
        now=NOW,
    ).candidates[0]
    candidates: list[TripCandidate] = []
    for index in range(size):
        data = base.model_dump(mode="json")
        data["id"] = f"benchmark-{index:05d}"
        data["quality"].update(
            {
                "driving_distance_km": 100.0 + float(index % 1000),
                "driving_duration_min": 60.0 + float(index % 500),
            }
        )
        candidates.append(TripCandidate.model_validate(data))
    return candidates


def benchmark(size: int, repeats: int) -> dict[str, float | int]:
    candidates = make_candidates(size)
    service = RecommendationService()
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = service.rank(candidates, RecommendationMode.BALANCED, now=NOW, limit=size)
        samples.append((time.perf_counter() - started) * 1_000.0)
    return {
        "candidates": size,
        "recommendations": len(result.recommendations),
        "median_ms": round(sorted(samples)[len(samples) // 2], 3),
        "max_ms": round(max(samples), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[50, 200])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")
    for size in args.sizes:
        if size < 1 or size > 200:
            raise SystemExit("benchmark sizes must be between 1 and 200")
        print(benchmark(size, args.repeats))


if __name__ == "__main__":
    main()
