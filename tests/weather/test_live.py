import asyncio
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.weather.kma_web import KmaWebBrowserClient, KmaWebWeatherProvider
from backend.weather.location import KmaLocation


LIVE = os.getenv("KTO_RUN_LIVE_WEATHER", "").strip().casefold() in {"1", "true", "yes", "on"}


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="set KTO_RUN_LIVE_WEATHER=1 for public-page validation")
def test_live_public_kma_http_provider_representative_regions():
    async def run():
        local_today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).date()
        provider = KmaWebWeatherProvider(request_interval_s=0.25)
        try:
            regions = {
                "Seoul": (37.5665, 126.9780),
                "Busan": (35.1796, 129.0756),
                "Gangneung": (37.7519, 128.8761),
                "Gyeongju": (35.8562, 129.2247),
            }
            result = {}
            for name, (lat, lng) in regions.items():
                days = await provider.daily_forecast(
                    lat,
                    lng,
                    local_today + timedelta(days=1),
                    local_today + timedelta(days=2),
                )
                assert len(days) == 2
                assert all(day.source == "kma_web" for day in days)
                assert all(day.source_url and day.content_fingerprint for day in days)
                result[name] = days
            return result
        finally:
            await provider.close()

    result = asyncio.run(run())
    assert set(result) == {"Seoul", "Busan", "Gangneung", "Gyeongju"}


@pytest.mark.live
@pytest.mark.skipif(
    not LIVE,
    reason="set KTO_RUN_LIVE_WEATHER=1 for browser fallback validation",
)
def test_live_public_kma_visible_dom_fallback():
    fixture = "<div id='digital-forecast'></div>"

    class Resolver:
        source_url = "https://www.weather.go.kr/w/rest/zone/find/dong.do"

        async def resolve(self, lat, lng):
            del lat, lng
            return KmaLocation(
                code="1114055000",
                name="서울특별시 중구 명동",
                x=60,
                y=127,
                lat=37.557236,
                lng=126.98789,
            )

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=fixture,
            request=request,
        )

    async def run():
        browser = KmaWebBrowserClient(request_interval_s=0)
        provider = KmaWebWeatherProvider(
            transport=httpx.MockTransport(handler),
            location_resolver=Resolver(),
            browser_client=browser,
            request_interval_s=0,
        )
        try:
            local_today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul")).date()
            days = await provider.daily_forecast(
                37.5,
                126.9,
                local_today,
                local_today,
            )
            return days, browser.browser_launch_count, provider.last_diagnostics
        finally:
            await provider.close()
            await browser.close()

    days, launches, diagnostics = asyncio.run(run())
    assert days and launches == 1
    assert diagnostics["parser_path"] == "visible_dom"
