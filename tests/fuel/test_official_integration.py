import asyncio
from datetime import datetime, timezone

import pytest

from backend.fuel.models import FuelType
from backend.fuel.official import BrowserFuelClient, HttpFuelClient


pytestmark = [pytest.mark.integration, pytest.mark.official]


def test_live_opinet_http_returns_current_national_average_for_all_fuels() -> None:
    async def run():
        client = HttpFuelClient()
        return [await client.get_price(fuel_type) for fuel_type in FuelType]

    results = asyncio.run(run())

    assert {result.fuel_type for result in results} == set(FuelType)
    for result in results:
        assert result.complete is True
        assert result.price_krw_per_l is not None and result.price_krw_per_l > 0
        assert result.unit.value == "krw_per_l"
        assert result.observed_at is not None
        assert result.observed_at <= datetime.now(timezone.utc)
        assert result.raw_evidence_hash and len(result.raw_evidence_hash) == 64


def test_live_opinet_browser_reads_visible_result_dom_for_all_fuels() -> None:
    async def run():
        client = BrowserFuelClient(request_interval_s=0, artifacts_dir=None)
        return [await client.get_price(fuel_type) for fuel_type in FuelType], client

    results, client = asyncio.run(run())

    assert {result.fuel_type for result in results} == set(FuelType)
    for result in results:
        assert result.complete is True
        assert result.price_krw_per_l is not None and result.price_krw_per_l > 0
        assert result.observed_at is not None
    assert all(event["kind"] in {"request", "response"} for event in client.last_request_events)
