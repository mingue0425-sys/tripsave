"""Chromium E2E for the V0.5 driving-cost lifecycle.

The Python Playwright runner is used because the application intentionally has
no Node runtime dependency. Set KTO_CHROME_PATH when the bundled Playwright
Chromium executable is not the default browser on the machine.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


BASE_URL = os.getenv("KTO_BASE_URL", "http://127.0.0.1:8765")
CHROME_PATH = os.getenv("KTO_CHROME_PATH") or os.getenv("KTO_FUEL_BROWSER_EXECUTABLE_PATH")
FORBIDDEN_TERMS = (
    "google",
    "kakao",
    "naver",
    "tmap",
    "mapbox",
    "router.project-osrm.org",
    "openrouteservice",
    "graphhopper",
    "analytics",
    "telemetry",
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[PASS] {message}")


async def choose_search(page, slot: str, query: str, expected_name: str) -> None:
    await page.locator(f"#{slot}-search-toggle").click()
    await page.locator(f"#{slot}-search-input").fill(query)
    await page.locator(f"#{slot}-search-results .search-result").first.wait_for(
        state="visible", timeout=10_000
    )
    await page.locator(f"#{slot}-search-results .search-result").first.click()
    await page.wait_for_function(
        "() => { const map = window.KoreaTripMap && window.KoreaTripMap.getMap(); "
        "return map && !map.isMoving(); }",
        timeout=10_000,
    )
    state = await page.evaluate("() => window.KoreaTripSelection.getState()")
    check(state[slot]["name"] == expected_name, f"{slot} selects {expected_name}")


async def wait_for_route(page) -> None:
    await page.wait_for_function(
        "() => { const state = window.KoreaTripRoute.getState(); "
        "return state.status === 'success' && state.route && state.route.distance_m > 0; }",
        timeout=30_000,
    )


async def wait_for_cost(page) -> None:
    await page.wait_for_function(
        "() => { const state = window.KoreaTripCost.getState(); "
        "return ['success', 'partial', 'error'].includes(state.status); }",
        timeout=120_000,
    )


async def main() -> None:
    requests: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    async with async_playwright() as playwright:
        launch_options = {"headless": True}
        if CHROME_PATH:
            launch_options["executable_path"] = CHROME_PATH
        browser = await playwright.chromium.launch(**launch_options)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        page.on("request", lambda request: requests.append(request.url))
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=30_000)
            await page.evaluate("() => localStorage.clear()")
            await page.reload(wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_function(
                "() => window.KoreaTripMap && window.KoreaTripMap.isReady()",
                timeout=30_000,
            )
            await page.wait_for_function(
                "() => { const map = window.KoreaTripMap.getMap(); "
                "return map && map.isStyleLoaded() && map.areTilesLoaded(); }",
                timeout=30_000,
            )

            await choose_search(page, "origin", "Seoul", "서울")
            await choose_search(page, "destination", "Daejeon", "대전")
            await page.locator("#route-calculate").click()
            await wait_for_route(page)
            route = await page.evaluate("() => window.KoreaTripRoute.getState().route")
            check(route["distance_m"] > 0, "OSRM route distance reaches the cost engine")

            await page.locator("#fuel-efficiency").fill("13.5")
            await page.locator("#fuel-type").select_option("gasoline")
            check(
                await page.locator("#driving-cost-calculate").is_enabled(),
                "cost button enables after route and efficiency",
            )
            await page.locator("#driving-cost-calculate").click()
            await wait_for_cost(page)
            first = await page.evaluate("() => window.KoreaTripCost.getState()")
            result = first["result"]
            check(result and result["fuel"]["complete"], "official gasoline price produces complete fuel cost")
            check(result["fuel"]["price_krw_per_l"] > 0, "gasoline price is positive, not zero fallback")
            check(result["fuel"]["fuel_volume_l"] > 0, "fuel volume uses canonical route distance")
            driving = result["driving_cost"]
            check(driving["cost_complete"], "toll plus fuel produces a complete driving cost")
            check(isinstance(driving["outbound"]["total_krw"], int), "outbound total is integer KRW")
            check(isinstance(driving["return"]["total_krw"], int), "return total is integer KRW")
            check(isinstance(driving["round_trip"]["total_krw"], int), "round-trip total is integer KRW")
            cost_leg_text = await page.locator(".driving-cost-metrics .cost-leg").all_inner_texts()
            check(len(cost_leg_text) == 3, "UI renders outbound, return, and round-trip sections")
            check(any("가는 길" in text for text in cost_leg_text), "UI labels the outbound leg")
            check(any("오는 길" in text for text in cost_leg_text), "UI labels the return leg")
            check(any("왕복 합계" in text for text in cost_leg_text), "UI labels the round-trip aggregate")
            check(
                await page.locator("#cost-outbound-fuel").inner_text() != "확인 불가",
                "UI renders outbound fuel cost",
            )
            check(
                await page.locator("#cost-return-fuel").inner_text() != "확인 불가",
                "UI renders return fuel cost",
            )
            check(
                await page.locator("#cost-outbound-total").inner_text()
                == f'{driving["outbound"]["total_krw"]:,}원',
                "UI outbound total matches the API JSON",
            )
            check(
                await page.locator("#cost-return-total").inner_text()
                == f'{driving["return"]["total_krw"]:,}원',
                "UI return total matches the API JSON",
            )
            check(
                await page.locator("#cost-round-trip-total").inner_text()
                == f'{driving["round_trip"]["total_krw"]:,}원',
                "UI round-trip total matches the API JSON",
            )
            if driving["round_trip_toll"]["estimated"]:
                check(
                    "추정" in await page.locator("#cost-return-toll").inner_text(),
                    "UI labels doubled outbound return toll as an estimate",
                )
            check(
                driving["round_trip"]["total_krw"]
                == driving["outbound"]["total_krw"] + driving["return"]["total_krw"],
                "round-trip total equals outbound plus return",
            )
            toll_state = await page.evaluate("() => window.KoreaTripToll.getState()")
            check(
                toll_state["result"] and toll_state["result"]["route_id"] == route["route_id"],
                "aggregate result synchronizes the official toll panel",
            )

            old_price = result["fuel"]["price_krw_per_l"]
            old_fuel_cost = result["fuel"]["one_way_krw"]
            await page.locator("#fuel-efficiency").fill("14.0")
            await page.locator("#fuel-efficiency").dispatch_event("change")
            await page.wait_for_function(
                "(old) => { const result = window.KoreaTripCost.getState().result; "
                "return result && result.fuel.fuel_efficiency_km_per_l === 14 && "
                "result.fuel.one_way_krw !== old; }",
                arg=old_fuel_cost,
                timeout=10_000,
            )
            changed = await page.evaluate("() => window.KoreaTripCost.getState().result")
            check(changed["fuel"]["price_krw_per_l"] == old_price, "efficiency change reuses cached verified price")
            check(
                changed["driving_cost"]["outbound"]["toll_krw"] == result["driving_cost"]["outbound"]["toll_krw"],
                "efficiency change preserves official toll",
            )

            await page.locator("#fuel-type").select_option("diesel")
            await page.wait_for_function(
                "() => window.KoreaTripCost.getState().result === null", timeout=10_000
            )
            await page.locator("#driving-cost-calculate").click()
            await wait_for_cost(page)
            diesel = await page.evaluate("() => window.KoreaTripCost.getState()")
            check(diesel["result"]["fuel"]["fuel_type"] == "diesel", "fuel type change recalculates diesel")
            check(
                diesel["result"]["fuel"]["complete"] and diesel["result"]["fuel"]["price_krw_per_l"] > 0,
                "official diesel price is used after fuel change",
            )

            await choose_search(page, "destination", "Busan", "부산")
            await page.wait_for_function(
                "() => window.KoreaTripRoute.getState().route === null && "
                "window.KoreaTripCost.getState().result === null",
                timeout=10_000,
            )
            check(True, "route change invalidates stale aggregate cost")

            local_host = urlsplit(BASE_URL).hostname
            allowed_hosts = {local_host, "tiles.openfreemap.org"}
            external = [
                url
                for url in requests
                if not url.startswith(("blob:", "data:"))
                and urlsplit(url).hostname not in allowed_hosts
            ]
            forbidden = [
                url
                for url in requests
                if any(term in url.casefold() for term in FORBIDDEN_TERMS)
            ]
            check(not external, "browser network has no unapproved external hosts")
            check(not forbidden, "browser network has no prohibited providers or telemetry")
            check(not console_errors and not page_errors, "Chromium has no console errors")
            print(
                {
                    "status": "PASS",
                    "request_count": len(requests),
                    "external_requests": external,
                    "forbidden_requests": forbidden,
                }
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
