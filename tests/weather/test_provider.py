import asyncio
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

import backend.weather.kma_web as kma_web
from backend.weather.kma_web import BrowserFetchResult, KmaWebBrowserClient, KmaWebWeatherProvider
from backend.weather.location import KmaLocation
from backend.weather.provider import (
    KmaWeatherProvider,
    WeatherProviderError,
    _condition,
    _daily_condition,
    _parse_precipitation_mm,
)


FIXTURE = (Path(__file__).with_name("fixtures") / "kma_web_forecast.html").read_text(
    encoding="utf-8"
)
FRAGMENT_URL = "https://www.weather.go.kr/w/wnuri-fct2021/main/digital-forecast.do"
PAGE_URL = "https://www.weather.go.kr/w/forecast/overall/short-term.do#dong/1114055000/37.5/126.9"


class FakeResolver:
    source_url = "https://www.weather.go.kr/w/rest/zone/find/dong.do"

    def __init__(self):
        self.calls = 0
        self.location = KmaLocation(
            code="1114055000",
            name="서울특별시 중구 명동",
            x=60,
            y=127,
            lat=37.557236,
            lng=126.98789,
        )

    async def resolve(self, lat, lng):
        del lat, lng
        self.calls += 1
        await asyncio.sleep(0.01)
        return self.location


class FakeBrowser:
    def __init__(self, html=FIXTURE):
        self.calls = 0
        self.html = html

    async def fetch(self, location):
        assert location.code == "1114055000"
        self.calls += 1
        return BrowserFetchResult(self.html, PAGE_URL, 200)


def make_provider(handler, *, browser=None, resolver=None):
    return KmaWebWeatherProvider(
        transport=httpx.MockTransport(handler),
        location_resolver=resolver or FakeResolver(),
        browser_client=browser,
        request_interval_s=0,
    )


def test_http_first_reads_public_html_without_browser_fallback():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=FIXTURE,
            request=request,
        )

    browser = FakeBrowser()
    provider = make_provider(handler, browser=browser)
    try:
        days = asyncio.run(
            provider.daily_forecast(37.5, 126.9, date(2026, 9, 16), date(2026, 9, 18))
        )
    finally:
        asyncio.run(provider.close())

    assert len(days) == 3
    assert requests[0].url.path == "/w/wnuri-fct2021/main/digital-forecast.do"
    assert requests[0].url.params["code"] == "1114055000"
    assert requests[0].url.params["unit"] == "m/s"
    assert requests[0].url.params["hr1"] == "Y"
    assert browser.calls == 0
    assert provider.last_diagnostics["parser_path"] == "http_html"
    assert days[0].source == "kma_web"
    assert days[0].source_url == str(requests[0].url)


def test_insufficient_http_html_is_the_only_browser_fallback_trigger():
    shell = "<main><div id='digital-forecast'></div></main>"

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=shell,
            request=request,
        )

    browser = FakeBrowser()
    provider = make_provider(handler, browser=browser)
    try:
        days = asyncio.run(
            provider.daily_forecast(37.5, 126.9, date(2026, 9, 16), date(2026, 9, 16))
        )
    finally:
        asyncio.run(provider.close())

    assert len(days) == 1
    assert browser.calls == 1
    assert provider.browser_fallback_count == 1
    assert provider.last_diagnostics["parser_path"] == "visible_dom"
    assert provider.last_diagnostics["fallback_used"] is True


@pytest.mark.parametrize("status", [403, 429])
def test_access_denial_does_not_retry_through_playwright(status):
    def handler(request):
        return httpx.Response(status, headers={"content-type": "text/html"}, request=request)

    class BrowserMustNotRun(FakeBrowser):
        async def fetch(self, location):
            raise AssertionError("access denial must not be bypassed")

    provider = make_provider(handler, browser=BrowserMustNotRun())
    try:
        with pytest.raises(WeatherProviderError) as raised:
            asyncio.run(
                provider.daily_forecast(37.5, 126.9, date(2026, 9, 16), date(2026, 9, 16))
            )
        assert raised.value.code == "KMA_WEB_ACCESS_DENIED"
    finally:
        asyncio.run(provider.close())


def test_server_failure_does_not_turn_into_a_browser_retry():
    def handler(request):
        return httpx.Response(500, headers={"content-type": "text/html"}, request=request)

    provider = make_provider(handler, browser=FakeBrowser())
    try:
        with pytest.raises(WeatherProviderError) as raised:
            asyncio.run(
                provider.daily_forecast(37.5, 126.9, date(2026, 9, 16), date(2026, 9, 16))
            )
        assert raised.value.code == "KMA_WEB_SOURCE_FAILED"
        assert provider.browser_fallback_count == 0
    finally:
        asyncio.run(provider.close())


def test_wrong_content_type_is_not_parsed_as_html():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"unexpected": True},
            request=request,
        )

    browser = FakeBrowser()
    provider = make_provider(handler, browser=browser)
    try:
        days = asyncio.run(
            provider.daily_forecast(37.5, 126.9, date(2026, 9, 16), date(2026, 9, 16))
        )
        assert days[0].date == date(2026, 9, 16)
        assert browser.calls == 1
        assert provider.last_diagnostics["http_error"] == "KMA_WEB_PAGE_CHANGED"
    finally:
        asyncio.run(provider.close())


def test_explicit_access_denial_body_does_not_trigger_browser_retry():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<main>Access Denied: automated access is forbidden</main>",
            request=request,
        )

    browser = FakeBrowser()
    provider = make_provider(handler, browser=browser)
    try:
        with pytest.raises(WeatherProviderError) as raised:
            asyncio.run(
                provider.daily_forecast(37.5, 126.9, date(2026, 9, 16), date(2026, 9, 16))
            )
        assert raised.value.code == "KMA_WEB_ACCESS_DENIED"
        assert browser.calls == 0
    finally:
        asyncio.run(provider.close())


def test_http_timeout_has_a_distinct_fail_safe_code():
    def handler(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    provider = make_provider(handler, browser=FakeBrowser())
    try:
        with pytest.raises(WeatherProviderError) as raised:
            asyncio.run(
                provider.daily_forecast(37.5, 126.9, date(2026, 9, 16), date(2026, 9, 16))
            )
        assert raised.value.code == "KMA_WEB_TIMEOUT"
        assert provider.browser_fallback_count == 0
    finally:
        asyncio.run(provider.close())


def test_parse_timeout_has_a_distinct_phase(monkeypatch):
    original = kma_web.parse_kma_web_html

    def slow_parser(*args, **kwargs):
        time.sleep(0.2)
        return original(*args, **kwargs)

    monkeypatch.setattr(kma_web, "parse_kma_web_html", slow_parser)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=FIXTURE,
            request=request,
        )

    provider = make_provider(handler, browser=None)
    provider.parse_timeout_s = 0.1
    try:
        with pytest.raises(WeatherProviderError) as raised:
            asyncio.run(
                provider.daily_forecast(37.5, 126.9, date(2026, 9, 16), date(2026, 9, 16))
            )
        assert raised.value.code == "KMA_WEB_TIMEOUT"
        assert raised.value.phase == "parse"
    finally:
        asyncio.run(provider.close())


def test_redirect_outside_allowlist_is_rejected():
    seen_hosts = []

    def handler(request):
        seen_hosts.append(request.url.host)
        if request.url.host == "www.weather.go.kr":
            return httpx.Response(
                302,
                headers={"location": "https://evil.example/forecast"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=FIXTURE,
            request=request,
        )

    provider = make_provider(handler, browser=FakeBrowser())
    try:
        with pytest.raises(WeatherProviderError) as raised:
            asyncio.run(
                provider.daily_forecast(37.5, 126.9, date(2026, 9, 16), date(2026, 9, 16))
            )
        assert raised.value.code == "KMA_WEB_REDIRECT_REJECTED"
        assert provider.browser_fallback_count == 0
        assert seen_hosts == ["www.weather.go.kr"]
    finally:
        asyncio.run(provider.close())


@pytest.mark.parametrize(
    "fragment_url",
    [
        "https://evil.example/forecast",
        "https://www.weather.go.kr:8443/w/wnuri-fct2021/main/digital-forecast.do",
        "https://www.weather.go.kr/w/wnuri-fct2021/main/digital-forecast.do?source=client",
    ],
)
def test_provider_source_url_is_server_fixed(fragment_url):
    with pytest.raises(ValueError):
        KmaWebWeatherProvider(fragment_url=fragment_url, browser_fallback_enabled=False)


def test_identical_concurrent_requests_use_one_live_fetch():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=FIXTURE,
            request=request,
        )

    resolver = FakeResolver()
    provider = make_provider(handler, resolver=resolver)

    async def run():
        return await asyncio.gather(
            *(
                provider.daily_forecast(
                    37.5, 126.9, date(2026, 9, 16), date(2026, 9, 16)
                )
                for _ in range(20)
            )
        )

    try:
        results = asyncio.run(run())
    finally:
        asyncio.run(provider.close())

    assert calls == 1
    assert resolver.calls == 1
    assert provider.http_fetch_count == 1
    assert provider._singleflight.owners == 1
    assert provider._singleflight.waiters == 19
    assert all(result[0].date == date(2026, 9, 16) for result in results)


def test_browser_pool_reuses_one_launch_and_closes_cleanly():
    class FakeBrowser:
        def __init__(self):
            self.launches = 0
            self.close_calls = 0

        def is_connected(self):
            return True

        async def close(self):
            self.close_calls += 1

    class FakeChromium:
        def __init__(self, browser):
            self.browser = browser

        async def launch(self, **kwargs):
            self.browser.launches += 1
            return self.browser

    class FakeRuntime:
        def __init__(self, browser):
            self.chromium = FakeChromium(browser)
            self.stop_calls = 0

        async def stop(self):
            self.stop_calls += 1

    class FakeManager:
        def __init__(self, runtime):
            self.runtime = runtime

        async def start(self):
            return self.runtime

    async def run():
        browser = FakeBrowser()
        runtime = FakeRuntime(browser)
        client = KmaWebBrowserClient(request_interval_s=0)
        factory = lambda: FakeManager(runtime)
        first = await client._ensure_browser(factory)
        second = await client._ensure_browser(factory)
        await client.close()
        return browser, runtime, first, second, client

    browser, runtime, first, second, client = asyncio.run(run())

    assert first is second
    assert browser.launches == 1
    assert browser.close_calls == 1
    assert runtime.stop_calls == 1
    assert client.browser_launch_count == 1


def test_api_precipitation_type_mapping_keeps_snow_distinct():
    assert _condition("1", "1") == "비"
    assert _condition("1", "2") == "비/눈"
    assert _condition("1", "3") == "눈"
    assert _condition("1", "4") == "소나기"


def test_api_precipitation_parser_is_conservative_for_ranges():
    assert _parse_precipitation_mm("강수없음") == 0.0
    assert _parse_precipitation_mm("0.0mm") == 0.0
    assert _parse_precipitation_mm("7.5mm") == 7.5
    assert _parse_precipitation_mm("1.0mm 미만") is None
    assert _parse_precipitation_mm("30~50mm") is None


def test_api_daily_condition_prefers_precipitation_over_late_sky_value():
    assert _daily_condition(
        [("0900", "1"), ("1500", "3"), ("2300", "4")],
        [("0900", "0"), ("1500", "1"), ("2300", "0")],
    ) == "비"


def test_api_daily_condition_uses_daytime_sky_not_late_night_sky():
    assert _daily_condition(
        [("0900", "1"), ("1200", "1"), ("1500", "3"), ("2300", "4")],
        [("0900", "0"), ("1200", "0"), ("1500", "0"), ("2300", "0")],
    ) == "맑음"


def test_api_daily_condition_combines_rain_and_snow_events():
    assert _daily_condition(
        [("1200", "4")],
        [("0900", "1"), ("1500", "3")],
    ) == "비/눈"


def test_api_base_time_waits_for_publication_delay():
    seoul = ZoneInfo("Asia/Seoul")
    assert KmaWeatherProvider._base_time(
        datetime(2026, 9, 16, 2, 5, tzinfo=seoul)
    ) == ("20260915", "2300")
    assert KmaWeatherProvider._base_time(
        datetime(2026, 9, 16, 2, 20, tzinfo=seoul)
    ) == ("20260916", "0200")
