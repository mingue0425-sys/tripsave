import asyncio
from datetime import datetime, timedelta, timezone

from backend.tolls.cache import TollRateCache
from backend.tolls.official import (
    BrowserTollClient,
    KoreaExpresswayTollCrawler,
    OfficialTollLookup,
    OfficialTollParserError,
    OfficialTollRouteNotFoundError,
    OfficialTollStationNotFoundError,
)
from backend.tolls.service import TollCalculator
from backend.tolls.models import TollVehicleClass

from .test_service import FakeIndex, request, supported_analysis


def lookup_value() -> OfficialTollLookup:
    return OfficialTollLookup(
        entry_name="서울",
        exit_name="부산",
        route_label="서울~부산",
        distance_km=10.0,
        prices={
            "class_1": 1_000,
            "class_2": 1_100,
            "class_3": 1_200,
            "class_4": 1_300,
            "class_5": 1_400,
            "compact": 500,
        },
        source_url="https://www.ex.co.kr/portal/usefee/selectUseFeeNList.do",
        fetched_at=datetime.now(timezone.utc),
        raw_evidence_hash="3" * 64,
    )


class SlowCrawler:
    def __init__(self) -> None:
        self.calls = 0
        self.last_source_path = "HTTP"
        self.value = lookup_value()

    async def lookup(self, entry_name: str, exit_name: str) -> OfficialTollLookup:
        self.calls += 1
        await asyncio.sleep(0.03)
        return self.value


def make_calculator(tmp_path, crawler, *, stale_max_age_days: int = 180):
    calculator = TollCalculator(
        index_path=tmp_path / "unused.db",
        cache=TollRateCache(
            tmp_path / "cache.db",
            ttl_days=30,
            stale_max_age_days=stale_max_age_days,
        ),
        crawler=crawler,
    )
    calculator.index = FakeIndex(supported_analysis())
    return calculator


def test_concurrent_duplicate_pair_uses_one_official_lookup(tmp_path) -> None:
    crawler = SlowCrawler()
    calculator = make_calculator(tmp_path, crawler)

    async def run():
        return await asyncio.gather(
            *(calculator.calculate(request()) for _ in range(10))
        )

    results = asyncio.run(run())
    assert crawler.calls == 1
    assert all(result.toll.complete for result in results)
    assert calculator.metrics["singleflight_owner"] == 1
    assert calculator.metrics["singleflight_waiter"] == 9


def test_pair_cache_reuses_full_vehicle_price_table(tmp_path) -> None:
    crawler = SlowCrawler()
    calculator = make_calculator(tmp_path, crawler)

    first = asyncio.run(calculator.calculate(request()))
    class_one_request = request().model_copy(
        update={"vehicle_class": TollVehicleClass.CLASS_1}
    )
    second = asyncio.run(calculator.calculate(class_one_request))

    assert first.toll.total_toll_krw == 500
    assert second.toll.total_toll_krw == 1_000
    assert crawler.calls == 1
    assert second.toll.diagnostics.source_path == "CACHE"


def test_stale_pair_returns_immediately_and_schedules_refresh(tmp_path) -> None:
    crawler = SlowCrawler()
    calculator = make_calculator(tmp_path, crawler)
    calculator.cache.put("서울", "부산", lookup_value())

    import sqlite3

    connection = sqlite3.connect(tmp_path / "cache.db")
    try:
        connection.execute(
            "UPDATE toll_rates_cache SET expires_at = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()

    async def run():
        result = await calculator.calculate(request())
        assert result.toll.source_status == "stale"
        assert result.toll.diagnostics.source_path == "STALE_CACHE"
        await asyncio.sleep(0.08)
        return result

    result = asyncio.run(run())
    assert result.toll.total_toll_krw == 500
    assert crawler.calls == 1


def test_http_primary_uses_browser_only_for_validated_http_failure() -> None:
    class FailingHttp:
        async def lookup(self, entry_name: str, exit_name: str):
            raise OfficialTollParserError("synthetic page change")

    class BrowserDouble:
        def __init__(self) -> None:
            self.calls = 0

        async def lookup_with_context(self, entry_name, exit_name, **kwargs):
            self.calls += 1
            return lookup_value()

    browser = BrowserDouble()
    crawler = KoreaExpresswayTollCrawler(
        http_client=FailingHttp(),
        browser_client=browser,
        prefer_http=True,
        allow_http_fallback=True,
    )

    result = asyncio.run(crawler.lookup_with_context("서울", "부산"))

    assert result.prices[TollVehicleClass.CLASS_1] == 1_000
    assert browser.calls == 1
    assert crawler.last_source_path == "PLAYWRIGHT"


def test_valid_official_partial_rows_do_not_trigger_browser_fallback() -> None:
    class NoRouteHttp:
        async def lookup(self, entry_name: str, exit_name: str):
            raise OfficialTollRouteNotFoundError("no complete route row")

    class BrowserMustNotRun:
        async def lookup_with_context(self, entry_name, exit_name, **kwargs):
            raise AssertionError("browser fallback must not run for a definitive no-route result")

    crawler = KoreaExpresswayTollCrawler(
        http_client=NoRouteHttp(),
        browser_client=BrowserMustNotRun(),
        prefer_http=True,
        allow_http_fallback=True,
    )

    async def run():
        try:
            await crawler.lookup_with_context("entry", "exit")
        except OfficialTollRouteNotFoundError:
            return
        raise AssertionError("the definitive no-route error should be preserved")

    asyncio.run(run())


def test_station_candidate_miss_does_not_trigger_browser_fallback() -> None:
    class MissingStationHttp:
        async def lookup_with_context(self, entry_name, exit_name, **kwargs):
            raise OfficialTollStationNotFoundError("candidate is not an official station")

    class BrowserMustNotRun:
        async def lookup_with_context(self, entry_name, exit_name, **kwargs):
            raise AssertionError("candidate station misses must not launch the browser")

    crawler = KoreaExpresswayTollCrawler(
        http_client=MissingStationHttp(),
        browser_client=BrowserMustNotRun(),
        prefer_http=True,
        allow_http_fallback=True,
    )

    async def run():
        try:
            await crawler.lookup_with_context("candidate", "exit")
        except OfficialTollStationNotFoundError:
            return
        raise AssertionError("the candidate miss should be preserved")

    asyncio.run(run())


def test_browser_pool_reuses_one_launch() -> None:
    class FakeBrowser:
        def __init__(self) -> None:
            self.launches = 0
            self.close_calls = 0

        def is_connected(self) -> bool:
            return True

        async def close(self) -> None:
            self.close_calls += 1

    class FakeChromium:
        def __init__(self, browser: FakeBrowser) -> None:
            self.browser = browser

        async def launch(self, **kwargs):
            self.browser.launches += 1
            return self.browser

    class FakeRuntime:
        def __init__(self, browser: FakeBrowser) -> None:
            self.chromium = FakeChromium(browser)
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1

    class FakeManager:
        def __init__(self, runtime: FakeRuntime) -> None:
            self.runtime = runtime

        async def start(self):
            return self.runtime

    async def run():
        browser = FakeBrowser()
        runtime = FakeRuntime(browser)
        client = BrowserTollClient()
        factory = lambda: FakeManager(runtime)
        first = await client._ensure_browser(factory)
        second = await client._ensure_browser(factory)
        await client.close()
        return browser, runtime, first, second

    browser, runtime, first, second = asyncio.run(run())

    assert first is second
    assert browser.launches == 1
    assert browser.close_calls == 1
    assert runtime.stop_calls == 1


def test_failed_pair_cooldown_preserves_unknown_and_avoids_repeat_lookup(tmp_path) -> None:
    class FailingCrawler:
        def __init__(self) -> None:
            self.calls = 0

        async def lookup(self, entry_name: str, exit_name: str):
            self.calls += 1
            raise OfficialTollParserError("no complete official route row")

    crawler = FailingCrawler()
    calculator = make_calculator(tmp_path, crawler)

    first = asyncio.run(calculator.calculate(request()))
    second = asyncio.run(calculator.calculate(request()))

    assert crawler.calls == 1
    assert first.toll.complete is False
    assert second.toll.complete is False
    assert first.toll.total_toll_krw is None
    assert second.toll.total_toll_krw is None
    assert second.toll.reason == "OFFICIAL_PARSE_FAILED"
    assert calculator.metrics["failure_cooldown_hit"] == 1
