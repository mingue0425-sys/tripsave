import asyncio
import json
from pathlib import Path

import pytest

from backend.tolls.models import TollVehicleClass
from backend.tolls.official import KoreaExpresswayTollCrawler


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
@pytest.mark.official
def test_live_official_html_lookup_returns_all_vehicle_columns() -> None:
    places = {
        place["id"]: place["name"]
        for place in json.loads(
            (PROJECT_ROOT / "static" / "data" / "places.json").read_text(
                encoding="utf-8"
            )
        )
    }
    pairs = [
        ("city-seoul", "city-busan"),
        ("city-seoul", "city-daejeon"),
        ("city-busan", "city-daegu"),
        ("city-gangneung", "city-seoul"),
        ("city-daejeon", "city-gwangju"),
    ]

    async def run():
        crawler = KoreaExpresswayTollCrawler()
        results = []
        for entry_id, exit_id in pairs:
            results.append(await crawler.lookup(places[entry_id], places[exit_id]))
        return results

    results = asyncio.run(run())
    expected_classes = set(TollVehicleClass)
    assert len(results) == len(pairs)
    for result in results:
        assert set(result.prices) == expected_classes
        assert all(isinstance(value, int) and value >= 0 for value in result.prices.values())
        assert result.prices[TollVehicleClass.CLASS_1] > 0
        assert result.distance_km is not None and result.distance_km > 0
        assert result.entry_official_id and result.exit_official_id
        assert len(result.raw_evidence_hash) == 64
