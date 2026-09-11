import asyncio

import httpx
import pytest

from backend.fuel.models import FuelPriceResult, FuelType
from backend.fuel.official import (
    FuelSourceError,
    HttpFuelClient,
    OfficialFuelPriceSource,
    validate_opinet_url,
)

from .test_parser import LIQUID_HTML, LPG_HTML


def test_http_client_reads_the_fixed_official_html_views() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        html = LPG_HTML if request.url.path.endswith("dopVsAvselSelect.do") else LIQUID_HTML
        return httpx.Response(200, text=html, request=request)

    async def run() -> tuple[FuelPriceResult, FuelPriceResult, FuelPriceResult]:
        client = HttpFuelClient(
            transport=httpx.MockTransport(handler),
            request_timeout_s=1,
            connect_timeout_s=1,
        )
        return (
            await client.get_price(FuelType.GASOLINE),
            await client.get_price(FuelType.DIESEL),
            await client.get_price(FuelType.LPG),
        )

    gasoline, diesel, lpg = asyncio.run(run())

    assert gasoline.price_krw_per_l == 1858.91
    assert diesel.price_krw_per_l == 1844.02
    assert lpg.price_krw_per_l == 1098.31
    assert [request.method for request in requests] == ["GET", "GET", "GET"]
    assert requests[0].url.host == "www.opinet.co.kr"
    assert requests[2].url.path.endswith("dopVsAvselSelect.do")


def test_official_source_falls_back_to_browser_client_after_http_failure() -> None:
    class FailingClient:
        async def get_price(self, fuel_type: FuelType) -> FuelPriceResult:
            raise FuelSourceError("synthetic HTTP failure")

    class BrowserStub:
        async def get_price(self, fuel_type: FuelType) -> FuelPriceResult:
            return FuelPriceResult(
                fuel_type=fuel_type,
                price_krw_per_l=1_700.0,
                source_url="https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
                source_status="fresh",
                complete=True,
                raw_evidence_hash="b" * 64,
            )

    result = asyncio.run(
        OfficialFuelPriceSource(
            http_client=FailingClient(),
            browser_client=BrowserStub(),
        ).get_price(FuelType.GASOLINE)
    )

    assert result.complete is True
    assert result.price_krw_per_l == 1_700.0


@pytest.mark.parametrize(
    "url",
    [
        "http://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
        "https://evil.example/user/dopospdrg/dopOsPdrgSelect.do",
        "https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do?x=1",
        "https://www.opinet.co.kr/private",
    ],
)
def test_source_url_allowlist_rejects_non_fixed_pages(url: str) -> None:
    with pytest.raises(ValueError):
        validate_opinet_url(url)
