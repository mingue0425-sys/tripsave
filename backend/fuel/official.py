"""Official public-web fuel price clients for Opinet."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from backend.async_lock import LoopLocalAsyncLock
from backend.fuel.models import FuelPriceResult, FuelType
from backend.fuel.parser import (
    FuelParseError,
    FuelUnitUnsupportedError,
    PARSER_VERSION,
    parse_opinet_fuel_html,
)
from config import (
    FUEL_BROWSER_ARTIFACT_DIR,
    FUEL_BROWSER_HEADLESS,
    FUEL_BROWSER_NAVIGATION_TIMEOUT_S,
    FUEL_BROWSER_RESULT_TIMEOUT_S,
    FUEL_BROWSER_SELECTOR_TIMEOUT_S,
    FUEL_LIQUID_PRICE_URL,
    FUEL_LPG_PRICE_URL,
    FUEL_REQUEST_INTERVAL_S,
    OPINET_HOSTNAME,
)


LOGGER = logging.getLogger(__name__)
OPINET_LPG_HOSTNAME = "www.opinet.co.kr"
OPINET_ALLOWED_PATHS = frozenset(
    {
        "/user/dopospdrg/dopOsPdrgSelect.do",
        "/user/dopvsavsel/dopVsAvselSelect.do",
    }
)
FUEL_USER_AGENT = "KoreaTripOptimizer/0.5 (+public Opinet HTML lookup)"


class FuelSourceError(RuntimeError):
    code = "FUEL_SOURCE_FAILED"


class FuelSourceTimeoutError(FuelSourceError):
    code = "FUEL_SOURCE_TIMEOUT"


class FuelAccessDeniedError(FuelSourceError):
    code = "FUEL_ACCESS_DENIED"


class FuelPageChangedError(FuelSourceError):
    code = "FUEL_PAGE_CHANGED"


class FuelParseFailedError(FuelSourceError):
    code = "FUEL_PARSE_FAILED"


class FuelUnitError(FuelSourceError):
    code = "FUEL_UNIT_UNSUPPORTED"


class FuelPriceClient(Protocol):
    async def get_price(self, fuel_type: FuelType) -> FuelPriceResult:
        ...


def validate_opinet_url(value: str) -> str:
    """Only allow the two fixed official HTML views used by this app."""

    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OPINET_HOSTNAME
        or parsed.path not in OPINET_ALLOWED_PATHS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Fuel source URL must be a fixed official Opinet HTML page.")
    return value


def source_url_for(fuel_type: FuelType) -> str:
    if fuel_type is FuelType.LPG:
        return FUEL_LPG_PRICE_URL
    return FUEL_LIQUID_PRICE_URL


def _wrap_parse_error(error: FuelParseError) -> FuelSourceError:
    if isinstance(error, FuelUnitUnsupportedError):
        return FuelUnitError(str(error))
    return FuelParseFailedError(str(error))


class HttpFuelClient:
    """HTTP client for the server-rendered official result pages.

    The HTTP path is used only after the browser investigation confirmed that
    the current public pages include the result table in the initial HTML.
    BrowserFuelClient remains available as a source fallback.
    """

    def __init__(
        self,
        *,
        liquid_url: str = FUEL_LIQUID_PRICE_URL,
        lpg_url: str = FUEL_LPG_PRICE_URL,
        request_timeout_s: float = 20.0,
        connect_timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.liquid_url = validate_opinet_url(liquid_url)
        self.lpg_url = validate_opinet_url(lpg_url)
        self.timeout = httpx.Timeout(request_timeout_s, connect=connect_timeout_s)
        self.transport = transport

    def _url(self, fuel_type: FuelType) -> str:
        return self.lpg_url if fuel_type is FuelType.LPG else self.liquid_url

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.status_code in {401, 403, 429}:
            raise FuelAccessDeniedError(
                f"Opinet denied the public fuel page (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            raise FuelSourceError(
                f"Opinet returned HTTP {response.status_code} for the fuel page."
            )

    async def get_price(self, fuel_type: FuelType) -> FuelPriceResult:
        url = self._url(fuel_type)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": FUEL_USER_AGENT, "Accept-Language": "ko-KR"},
                transport=self.transport,
            ) as client:
                response = await client.get(url)
        except (httpx.TimeoutException, TimeoutError) as error:
            raise FuelSourceTimeoutError("The Opinet fuel page request timed out.") from error
        except httpx.HTTPError as error:
            raise FuelSourceError("The Opinet fuel page request failed.") from error
        self._check_response(response)
        try:
            return parse_opinet_fuel_html(
                response.text,
                fuel_type,
                source_url=url,
            )
        except FuelParseError as error:
            raise _wrap_parse_error(error) from error


class BrowserFuelClient:
    """Playwright fallback that reads the same visible public result DOM."""

    def __init__(
        self,
        *,
        liquid_url: str = FUEL_LIQUID_PRICE_URL,
        lpg_url: str = FUEL_LPG_PRICE_URL,
        navigation_timeout_s: float = FUEL_BROWSER_NAVIGATION_TIMEOUT_S,
        selector_timeout_s: float = FUEL_BROWSER_SELECTOR_TIMEOUT_S,
        result_timeout_s: float = FUEL_BROWSER_RESULT_TIMEOUT_S,
        request_interval_s: float = FUEL_REQUEST_INTERVAL_S,
        headless: bool = FUEL_BROWSER_HEADLESS,
        executable_path: str | None = None,
        artifacts_dir: Path | None = FUEL_BROWSER_ARTIFACT_DIR,
    ) -> None:
        self.liquid_url = validate_opinet_url(liquid_url)
        self.lpg_url = validate_opinet_url(lpg_url)
        self.navigation_timeout_s = navigation_timeout_s
        self.selector_timeout_s = selector_timeout_s
        self.result_timeout_s = result_timeout_s
        self.request_interval_s = max(0.0, request_interval_s)
        self.headless = headless
        self.executable_path = executable_path or os.getenv(
            "KTO_FUEL_BROWSER_EXECUTABLE_PATH",
            os.getenv("KTO_TOLL_BROWSER_EXECUTABLE_PATH"),
        )
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None
        self._lock = LoopLocalAsyncLock()
        self._next_lookup_at = 0.0
        self.last_request_events: list[dict[str, object]] = []
        self.blocked_external_urls: list[str] = []

    def _url(self, fuel_type: FuelType) -> str:
        return self.lpg_url if fuel_type is FuelType.LPG else self.liquid_url

    async def _wait_between_lookups(self) -> None:
        delay = self._next_lookup_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_lookup_at = time.monotonic() + self.request_interval_s

    @staticmethod
    def _request_event(request: Any) -> dict[str, object]:
        try:
            post_data = request.post_data
        except Exception:
            post_data = None
        return {
            "kind": "request",
            "method": request.method,
            "url": request.url,
            "post_body": post_data,
        }

    def _attach_network(self, page: Any) -> None:
        def on_request(request: Any) -> None:
            event = self._request_event(request)
            self.last_request_events.append(event)
            LOGGER.info(
                "official fuel request method=%s url=%s post_body=%s",
                event["method"],
                event["url"],
                event["post_body"],
            )

        def on_response(response: Any) -> None:
            try:
                headers = response.headers
            except Exception:
                headers = {}
            event = {
                "kind": "response",
                "method": response.request.method,
                "url": response.url,
                "status": response.status,
                "content_type": headers.get("content-type", ""),
                "redirect": headers.get("location"),
            }
            self.last_request_events.append(event)
            LOGGER.info(
                "official fuel response status=%s method=%s url=%s content_type=%s redirect=%s",
                event["status"],
                event["method"],
                event["url"],
                event["content_type"],
                event["redirect"],
            )

        page.on("request", on_request)
        page.on("response", on_response)

    async def _route_request(self, route: Any) -> None:
        hostname = urlsplit(route.request.url).hostname
        if hostname in {OPINET_HOSTNAME, "nfl.opinet.co.kr"}:
            await route.continue_()
            return
        self.blocked_external_urls.append(route.request.url)
        await route.abort()

    async def _capture(self, page: Any, name: str) -> None:
        if self.artifacts_dir is None:
            return
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(
                path=str(self.artifacts_dir / f"{name}.png"), full_page=True
            )
            if name == "after-search":
                (self.artifacts_dir / "result-fragment.html").write_text(
                    await page.locator("table").first.evaluate("(element) => element.outerHTML"),
                    encoding="utf-8",
                )
        except Exception as error:
            LOGGER.warning("Could not save fuel debug artifact %s: %s", name, error)

    @staticmethod
    def _check_browser_response(response: Any) -> None:
        if response is None:
            return
        if response.status in {401, 403, 429}:
            raise FuelAccessDeniedError(
                f"Opinet denied the browser fuel page (HTTP {response.status})."
            )
        if response.status >= 400:
            raise FuelSourceError(f"Opinet returned HTTP {response.status}.")

    async def get_price(self, fuel_type: FuelType) -> FuelPriceResult:
        url = self._url(fuel_type)
        async with self._lock:
            await self._wait_between_lookups()
            try:
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError
                from playwright.async_api import async_playwright
            except ImportError as error:
                raise FuelSourceError("Playwright is required for browser fuel lookup.") from error
            self.last_request_events = []
            self.blocked_external_urls = []
            try:
                async with async_playwright() as playwright:
                    launch_options: dict[str, object] = {"headless": self.headless}
                    if self.executable_path:
                        launch_options["executable_path"] = self.executable_path
                    browser = await playwright.chromium.launch(**launch_options)
                    context = None
                    try:
                        context = await browser.new_context(
                            user_agent=FUEL_USER_AGENT,
                            locale="ko-KR",
                        )
                        await context.route("**/*", self._route_request)
                        page = await context.new_page()
                        self._attach_network(page)
                        response = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=int(self.navigation_timeout_s * 1000),
                        )
                        self._check_browser_response(response)
                        await self._capture(page, "before-search")
                        selector = "#tbody1" if fuel_type is FuelType.LPG else "#numbox"
                        await page.locator(selector).wait_for(
                            state="attached",
                            timeout=int(self.selector_timeout_s * 1000),
                        )
                        # The initial GET is the site's normal current-price
                        # result.  Check that its visible query control still
                        # exists, then parse the result DOM; no private request
                        # or undocumented endpoint is used.
                        query_selector = (
                            "#dopVsAvselSelect" if fuel_type is FuelType.LPG else "#btn_search"
                        )
                        await page.locator(query_selector).wait_for(
                            state="visible",
                            timeout=int(self.selector_timeout_s * 1000),
                        )
                        try:
                            await page.locator("table").first.wait_for(
                                state="visible",
                                timeout=int(self.result_timeout_s * 1000),
                            )
                        except PlaywrightTimeoutError as error:
                            await self._capture(page, "after-search")
                            raise FuelPageChangedError(
                                "The Opinet fuel result table did not become visible."
                            ) from error
                        await self._capture(page, "after-search")
                        try:
                            return parse_opinet_fuel_html(
                                await page.content(),
                                fuel_type,
                                source_url=url,
                            )
                        except FuelUnitUnsupportedError as error:
                            raise FuelUnitError(str(error)) from error
                        except FuelParseError as error:
                            raise FuelParseFailedError(str(error)) from error
                    finally:
                        if context is not None:
                            try:
                                await asyncio.wait_for(context.close(), timeout=5)
                            except Exception as error:
                                LOGGER.warning("Could not close fuel context: %s", error)
                        try:
                            await asyncio.wait_for(browser.close(), timeout=5)
                        except Exception as error:
                            LOGGER.warning("Could not close fuel browser: %s", error)
            except FuelSourceError:
                raise
            except Exception as error:
                if "timeout" in str(error).casefold() or error.__class__.__name__.endswith(
                    "TimeoutError"
                ):
                    raise FuelSourceTimeoutError("The Opinet browser fuel flow timed out.") from error
                raise FuelSourceError("The Opinet browser fuel flow failed.") from error


class OfficialFuelPriceSource:
    """Cache-facing source facade: verified HTTP path, then browser fallback."""

    def __init__(
        self,
        *,
        http_client: FuelPriceClient | None = None,
        browser_client: FuelPriceClient | None = None,
        prefer_http: bool = True,
    ) -> None:
        self.http_client = http_client or HttpFuelClient()
        self.browser_client = browser_client or BrowserFuelClient()
        self.prefer_http = prefer_http
        self.last_error: FuelSourceError | None = None

    async def get_price(self, fuel_type: FuelType) -> FuelPriceResult:
        clients = (
            (self.http_client, self.browser_client)
            if self.prefer_http
            else (self.browser_client, self.http_client)
        )
        last_error: FuelSourceError | None = None
        for client in clients:
            if client is None:
                continue
            try:
                result = await client.get_price(fuel_type)
                self.last_error = None
                return result
            except FuelSourceError as error:
                last_error = error
                LOGGER.warning(
                    "Fuel source client failed fuel_type=%s code=%s: %s",
                    fuel_type.value,
                    error.code,
                    error,
                )
        self.last_error = last_error or FuelSourceError("No fuel source client is configured.")
        raise self.last_error
