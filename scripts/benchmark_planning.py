"""Small deterministic benchmark for the V1.1 pure planning layers."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.itinerary.models import (
    MatrixEntry,
    OptimizationMode,
    OptimizeRouteRequest,
    RoutePoint,
    RouteWaypoint,
)
from backend.itinerary.optimizer import optimize_matrix
from backend.models import Location
from backend.poi.models import PoiCategory, PoiRecord, PoiSearchRequest
from backend.poi.repository import PoiRepository


def benchmark_itinerary(count: int) -> float:
    origin = RoutePoint(id="origin", name="origin", lat=35.0, lng=127.0)
    destination = RoutePoint(id="destination", name="destination", lat=35.5, lng=127.5)
    waypoints = [RouteWaypoint(id=f"w-{index:02d}", name=f"W{index:02d}", lat=35.05 + index * 0.01, lng=127.05 + index * 0.01) for index in range(count)]
    points = [origin, *waypoints, destination]
    entries = [
        MatrixEntry(from_id=first.id, to_id=second.id, distance_m=1000 + abs(index - other_index) * 10, duration_s=120 + abs(index - other_index), fuel_krw=100, toll_krw=50)
        for index, first in enumerate(points)
        for other_index, second in enumerate(points)
        if index != other_index
    ]
    request = OptimizeRouteRequest(origin=origin, destination=destination, waypoints=waypoints, mode=OptimizationMode.FASTEST)
    started = time.perf_counter()
    optimize_matrix(request, entries)
    return (time.perf_counter() - started) * 1000


def benchmark_poi() -> float:
    with tempfile.TemporaryDirectory() as directory:
        repository = PoiRepository(Path(directory) / "poi.sqlite3")
        fetched_at = datetime(2026, 9, 16, tzinfo=timezone.utc)
        records = [
            PoiRecord(id=f"poi-{index}", source="benchmark", name=f"POI {index}", category=PoiCategory.HOSPITAL, lat=35.0 + (index % 100) * 0.0001, lng=127.0 + (index % 100) * 0.0001, fetched_at=fetched_at)
            for index in range(1000)
        ]
        repository.replace_records(records)
        request = PoiSearchRequest(destination=Location(lat=35.0, lng=127.0), categories=[PoiCategory.HOSPITAL], destination_radius_m=3000, limit_per_category=50)
        started = time.perf_counter()
        repository.search(request)
        return (time.perf_counter() - started) * 1000


def main() -> int:
    results = {"itinerary_5_waypoints_ms": benchmark_itinerary(5), "itinerary_10_waypoints_ms": benchmark_itinerary(10), "itinerary_20_waypoints_ms": benchmark_itinerary(20), "poi_1000_records_ms": benchmark_poi()}
    print(json.dumps(results, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
