import asyncio

import httpx
import pytest

from backend.tolls.official import (
    KoreaExpresswayTollCrawler,
    OfficialTollAccessDeniedError,
    OfficialTollParserError,
    OfficialTollTimeoutError,
)


HTML_FRAGMENT = """
<div id="noMinja">
  <table>
    <tr><th>구분</th><th>1종</th><th>2종</th><th>3종</th><th>4종</th><th>5종</th><th>1종(경차)</th></tr>
    <tr><td>서울~부산</td><td>18,600원</td><td>19,000원</td><td>19,700원</td><td>26,100원</td><td>30,700원</td><td>9,300원</td></tr>
  </table>
  <span id="range">385.8Km</span>
</div>
"""


def test_crawler_uses_normal_html_form_and_utf8_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if request.method == "GET":
            return httpx.Response(200, text="<html>form</html>", request=request)
        assert request.headers["content-type"].startswith(
            "application/x-www-form-urlencoded"
        )
        assert b"zonename1=%EC%84%9C%EC%9A%B8" in request.content
        assert b"zonename2=%EB%B6%80%EC%82%B0" in request.content
        return httpx.Response(200, text=HTML_FRAGMENT, request=request)

    async def run() -> object:
        crawler = KoreaExpresswayTollCrawler(
            request_interval_s=0,
            transport=httpx.MockTransport(handler),
        )
        return await crawler.lookup("서울", "부산")

    result = asyncio.run(run())
    assert result.prices[next(key for key in result.prices if key.value == "class_1")] == 18_600
    assert [request.url.path for request in requests] == [
        "/robots.txt",
        "/portal/usefee/selectUseFeeNList.do",
        "/portal/usefee/selectUseFeeNList.do",
    ]


@pytest.mark.parametrize(
    "status_code,error_type",
    [(403, OfficialTollAccessDeniedError), (429, OfficialTollAccessDeniedError)],
)
def test_crawler_maps_access_denial(status_code: int, error_type: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    async def run() -> object:
        crawler = KoreaExpresswayTollCrawler(
            request_interval_s=0,
            transport=httpx.MockTransport(handler),
        )
        return await crawler.lookup("서울", "부산")

    with pytest.raises(error_type):
        asyncio.run(run())


def test_crawler_maps_timeout_without_retry_loop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    async def run() -> object:
        crawler = KoreaExpresswayTollCrawler(
            request_interval_s=0,
            transport=httpx.MockTransport(handler),
        )
        return await crawler.lookup("서울", "부산")

    with pytest.raises(OfficialTollTimeoutError):
        asyncio.run(run())


def test_crawler_surfaces_parser_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><div id='noMinja'></div></html>", request=request)

    async def run() -> object:
        crawler = KoreaExpresswayTollCrawler(
            request_interval_s=0,
            transport=httpx.MockTransport(handler),
        )
        return await crawler.lookup("서울", "부산")

    with pytest.raises(OfficialTollParserError):
        asyncio.run(run())
