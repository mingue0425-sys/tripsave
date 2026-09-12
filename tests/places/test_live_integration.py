"""Opt-in live checks for the normal public VisitKorea HTML source."""

from __future__ import annotations

import asyncio

import pytest

from crawler.places.base import PlaceDestination
from crawler.places.visitkorea import VisitKoreaSource


@pytest.mark.integration
def test_visitkorea_returns_restaurant_and_attraction_records_for_three_cities() -> None:
    async def collect():
        source = VisitKoreaSource(max_results=6, request_interval_seconds=0.2)
        destinations = [
            PlaceDestination(37.5665, 126.978, "Seoul"),
            PlaceDestination(35.1796, 129.0756, "Busan"),
            PlaceDestination(35.8562, 129.2248, "Gyeongju"),
        ]
        output = {}
        for destination in destinations:
            categories = {}
            for category, radius in (("restaurant", 10.0), ("attraction", 30.0)):
                records = await source.search(destination, category, radius)
                categories[category] = records
            output[destination.label] = categories
        return output

    result = asyncio.run(collect())
    assert set(result) == {"Seoul", "Busan", "Gyeongju"}
    for categories in result.values():
        assert categories["restaurant"]
        assert categories["attraction"]
        for records in categories.values():
            assert all(record.source == "visitkorea" for record in records)
            assert all(record.name and record.source_url for record in records)
