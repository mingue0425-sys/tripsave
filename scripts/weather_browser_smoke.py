"""Browser smoke test for the local TripSave weather flow.

Start FastAPI separately with the normal no-key configuration, then run this
script.  The browser talks only to the local API; KMA traffic is server-side.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright


BASE_URL = "http://127.0.0.1:8765/"


async def main() -> None:
    today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul")).date()
    start = (today + timedelta(days=1)).isoformat()
    end = (today + timedelta(days=2)).isoformat()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("#weather-search")
        assert await page.evaluate(
            """() => window.KoreaTripSelection.setDestination({
                lat: 37.5665, lng: 126.978, label: '서울', source: 'local_search'
            })"""
        )
        await page.locator("#weather-start-date").fill(start)
        await page.locator("#weather-end-date").fill(end)
        cold_started = time.perf_counter()
        await page.locator("#weather-search").click()
        await page.locator(".weather-day").first.wait_for(state="visible", timeout=30_000)
        cold_s = time.perf_counter() - cold_started
        assert "기상청 공개 날씨누리" in await page.locator("#weather-results").inner_text()

        warm_started = time.perf_counter()
        await page.locator("#weather-search").click()
        await page.locator(".weather-day").first.wait_for(state="visible", timeout=30_000)
        warm_s = time.perf_counter() - warm_started

        await page.evaluate(
            """() => window.KoreaTripSelection.setDestination({
                lat: 35.1796, lng: 129.0756, label: '부산', source: 'local_search'
            })"""
        )
        await page.wait_for_function(
            "() => document.querySelectorAll('.weather-day').length === 0"
        )
        assert "부산" in await page.locator("#weather-summary").inner_text()
        assert any("/api/weather/forecast" in url for url in requests)
        assert not any("weather.go.kr" in url for url in requests)
        print(
            {
                "cold_http_or_cache_miss_s": round(cold_s, 3),
                "warm_cache_s": round(warm_s, 3),
                "destination_invalidation": "pass",
                "frontend_kma_requests": 0,
            }
        )
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
