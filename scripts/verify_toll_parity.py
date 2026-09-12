"""Compare HTTP-primary parsing with the real Playwright official flow."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tolls.official import (  # noqa: E402
    BrowserTollClient,
    HttpTollClient,
    OfficialStationStore,
    OfficialTollError,
)
from config import TOLL_INDEX_DB  # noqa: E402


PAIRS = (
    ("동서울", "대동"),
    ("서울", "유성"),
    ("대동", "수성"),
    ("강릉", "초월"),
    ("계룡", "광주"),
)


async def main() -> None:
    station_store = OfficialStationStore(TOLL_INDEX_DB)
    http = HttpTollClient(request_interval_s=0, station_store=station_store)
    browser = BrowserTollClient(
        request_interval_s=0,
        station_store=station_store,
    )
    results: list[dict[str, object]] = []
    try:
        await browser.warmup()
        for entry, exit in PAIRS:
            item: dict[str, object] = {"entry": entry, "exit": exit}
            try:
                http_result = await http.lookup(entry, exit)
                browser_result = await browser.lookup(entry, exit)
                item.update(
                    {
                        "http_ids": [
                            http_result.entry_official_id,
                            http_result.exit_official_id,
                        ],
                        "browser_ids": [
                            browser_result.entry_official_id,
                            browser_result.exit_official_id,
                        ],
                        "http_prices": {
                            key.value: value for key, value in http_result.prices.items()
                        },
                        "browser_prices": {
                            key.value: value for key, value in browser_result.prices.items()
                        },
                        "prices_equal": http_result.prices == browser_result.prices,
                        "distance_equal": http_result.distance_km == browser_result.distance_km,
                        "route_equal": (
                            http_result.entry_official_id == browser_result.entry_official_id
                            and http_result.exit_official_id == browser_result.exit_official_id
                        ),
                    }
                )
            except OfficialTollError as error:
                item.update({"error_code": error.code, "error": str(error)})
            results.append(item)
    finally:
        await http.close()
        await browser.close()

    compact_results: list[dict[str, object]] = []
    for item in results:
        compact: dict[str, object] = {
            "entry": item["entry"],
            "exit": item["exit"],
        }
        if item.get("error_code"):
            compact.update(
                {
                    "error_code": item["error_code"],
                    "error": item.get("error"),
                }
            )
        else:
            compact.update(
                {
                    "http_ids": item["http_ids"],
                    "browser_ids": item["browser_ids"],
                    "http_prices": item["http_prices"],
                    "browser_prices": item["browser_prices"],
                    "prices_equal": item["prices_equal"],
                    "distance_equal": item["distance_equal"],
                    "route_equal": item["route_equal"],
                }
            )
        compact_results.append(compact)

    passed = not any(
        item.get("error_code")
        or not item.get("prices_equal")
        or not item.get("distance_equal")
        or not item.get("route_equal")
        for item in results
    )
    print(json.dumps({"passed": passed, "results": compact_results}, ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
