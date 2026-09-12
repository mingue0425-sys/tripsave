"""Measure warm aggregate and verified HTTP toll timings.

The script does not clear the production database. Use the companion
clear_toll_pair_cache.py utility against an explicitly selected official pair
when a cold-cache run is required.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tolls.official import (
    BrowserTollClient,
    HttpTollClient,
    KoreaExpresswayTollCrawler,
    OfficialStationStore,
    OfficialTollError,
    OfficialTollPageChangedError,
)
from config import TOLL_INDEX_DB


ROUTES = {
    "서울→부산": (
        {"lat": 37.5665, "lng": 126.978, "label": "서울", "source": "benchmark"},
        {"lat": 35.1796, "lng": 129.0756, "label": "부산", "source": "benchmark"},
    ),
    "서울→대전": (
        {"lat": 37.5665, "lng": 126.978, "label": "서울", "source": "benchmark"},
        {"lat": 36.3504, "lng": 127.3845, "label": "대전", "source": "benchmark"},
    ),
    "부산→대구": (
        {"lat": 35.1796, "lng": 129.0756, "label": "부산", "source": "benchmark"},
        {"lat": 35.8714, "lng": 128.6014, "label": "대구", "source": "benchmark"},
    ),
    "강릉→서울": (
        {"lat": 37.7519, "lng": 128.8761, "label": "강릉", "source": "benchmark"},
        {"lat": 37.5665, "lng": 126.978, "label": "서울", "source": "benchmark"},
    ),
    "대전→광주": (
        {"lat": 36.3504, "lng": 127.3845, "label": "대전", "source": "benchmark"},
        {"lat": 35.1595, "lng": 126.8526, "label": "광주", "source": "benchmark"},
    ),
}


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 2)


def compact_diagnostics(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key in (
        "stage",
        "failure_stage",
        "failure_code",
        "official_lookup",
        "source_path",
        "entry_candidate_id",
        "exit_candidate_id",
        "official_entry",
        "official_exit",
        "timings_ms",
        "request_events",
    ):
        if key in value:
            result[key] = value[key]
    details = value.get("candidate_details")
    if isinstance(details, list):
        # Candidate evidence belongs in the route debug endpoint.  Benchmark
        # output should stay small enough to compare timings without losing
        # the useful count of spatial observations.
        result["candidate_count"] = len(details)
    if isinstance(value.get("notes"), list):
        result["notes"] = value["notes"]
    return result


async def post_json(
    client: httpx.AsyncClient, url: str, payload: dict[str, object]
) -> dict[str, object]:
    response = await client.post(url, json=payload)
    body = response.json()
    if response.status_code >= 400 or not isinstance(body, dict):
        raise RuntimeError(f"{response.status_code}: {body!r}")
    return body


async def benchmark_route(
    client: httpx.AsyncClient,
    base_url: str,
    origin: dict[str, object],
    destination: dict[str, object],
    repeat: int,
    include_diagnostics: bool = False,
) -> dict[str, object]:
    route_started = time.perf_counter()
    route_response = await post_json(
        client,
        f"{base_url}/api/routes",
        {"origin": origin, "destination": destination},
    )
    route_ms = (time.perf_counter() - route_started) * 1000
    route = route_response["route"]
    payload = {
        "route_id": route.get("route_id"),
        "origin": origin,
        "destination": destination,
        "route": route,
        "fuel_type": "gasoline",
        "fuel_efficiency_km_per_l": 15.0,
        "vehicle_class": "class_1",
        "round_trip_mode": "directional",
    }
    aggregate_ms: list[float] = []
    sources: list[str] = []
    diagnostic_samples: list[dict[str, object]] = []
    for _ in range(max(1, repeat)):
        started = time.perf_counter()
        body = await post_json(client, f"{base_url}/api/costs/driving", payload)
        aggregate_ms.append((time.perf_counter() - started) * 1000)
        toll = body.get("toll") or {}
        toll_diagnostics = toll.get("diagnostics") or {}
        sources.append(str(toll_diagnostics.get("source_path") or "UNKNOWN"))
        if include_diagnostics:
            diagnostic_samples.append(
                {
                    "toll": compact_diagnostics(toll_diagnostics),
                    "return_toll": compact_diagnostics(
                        (body.get("return_toll") or {}).get("diagnostics")
                    ),
                }
            )
    result = {
        "route_ms": round(route_ms, 2),
        "aggregate_ms": [round(value, 2) for value in aggregate_ms],
        "aggregate_p50_ms": percentile(aggregate_ms, 0.50),
        "aggregate_p95_ms": percentile(aggregate_ms, 0.95),
        "toll_source_paths": sources,
    }
    if include_diagnostics:
        result["diagnostics"] = diagnostic_samples
    return result


async def benchmark_http_pair(entry: str, exit: str, interval_s: float) -> dict[str, object]:
    source = HttpTollClient(request_interval_s=interval_s)
    started = time.perf_counter()
    try:
        lookup = await source.lookup(entry, exit)
        return {
            "entry": lookup.entry_name,
            "exit": lookup.exit_name,
            "entry_id": lookup.entry_official_id,
            "exit_id": lookup.exit_official_id,
            "distance_km": lookup.distance_km,
            "prices": {key.value: value for key, value in lookup.prices.items()},
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "timings_ms": dict(source.last_timings),
            "http_request_count": len(source.last_request_events),
        }
    except OfficialTollError as error:
        return {
            "entry": entry,
            "exit": exit,
            "error_code": error.code,
            "error": str(error),
            "timings_ms": dict(source.last_timings),
            "result_route_labels": list(source.last_result_route_labels),
            "http_request_count": len(source.last_request_events),
        }
    finally:
        await source.close()


async def benchmark_browser_fallback(entry: str, exit: str) -> dict[str, object]:
    class ForcedHttpFailure:
        async def lookup(self, entry_name: str, exit_name: str):
            raise OfficialTollPageChangedError("forced benchmark fallback")

    browser = BrowserTollClient(
        request_interval_s=0,
        station_store=OfficialStationStore(TOLL_INDEX_DB),
    )
    crawler = KoreaExpresswayTollCrawler(
        http_client=ForcedHttpFailure(),
        browser_client=browser,
        prefer_http=True,
        allow_http_fallback=True,
    )
    warmup_started = time.perf_counter()
    started = time.perf_counter()
    try:
        await crawler.warmup_browser()
        warmup_ms = round((time.perf_counter() - warmup_started) * 1000, 2)
        lookup = await crawler.lookup(entry, exit)
        return {
            "entry": lookup.entry_name,
            "exit": lookup.exit_name,
            "entry_id": lookup.entry_official_id,
            "exit_id": lookup.exit_official_id,
            "prices": {key.value: value for key, value in lookup.prices.items()},
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "warmup_ms": warmup_ms,
            "source_path": crawler.last_source_path,
            "browser_timings_ms": dict(browser.last_timings),
        }
    finally:
        await crawler.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--entry")
    parser.add_argument("--exit")
    parser.add_argument("--http-interval", type=float, default=0.0)
    parser.add_argument("--include-diagnostics", action="store_true")
    parser.add_argument("--route-index", type=int)
    parser.add_argument("--browser-fallback", action="store_true")
    args = parser.parse_args()
    if bool(args.entry) != bool(args.exit):
        parser.error("--entry and --exit must be supplied together")

    if args.browser_fallback:
        if not args.entry or not args.exit:
            parser.error("--browser-fallback requires --entry and --exit")
        print(
            json.dumps(
                await benchmark_browser_fallback(args.entry, args.exit),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.entry:
        print(
            json.dumps(
                await benchmark_http_pair(args.entry, args.exit, args.http_interval),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    async with httpx.AsyncClient(timeout=60.0) as client:
        results = {}
        route_items = list(ROUTES.items())
        if args.route_index is not None:
            if args.route_index < 0 or args.route_index >= len(route_items):
                parser.error(f"--route-index must be between 0 and {len(route_items) - 1}")
            route_items = [route_items[args.route_index]]
        for name, (origin, destination) in route_items:
            results[name] = await benchmark_route(
                client,
                args.base_url.rstrip("/"),
                origin,
                destination,
                args.repeat,
                args.include_diagnostics,
            )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
