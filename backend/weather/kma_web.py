"""HTTP-first provider for the public KMA 날씨누리 forecast screen."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from backend.async_lock import LoopLocalAsyncLock
from config import (
    WEATHER_KMA_WEB_ALLOWED_HOSTS,
    WEATHER_KMA_WEB_BROWSER_HEADLESS,
    WEATHER_KMA_WEB_BROWSER_DEPENDENCY_HOSTS,
    WEATHER_KMA_WEB_CONNECT_TIMEOUT_S,
    WEATHER_KMA_WEB_FRAGMENT_URL,
    WEATHER_KMA_WEB_HTTP_TIMEOUT_S,
    WEATHER_KMA_WEB_MAX_RESPONSE_BYTES,
    WEATHER_KMA_WEB_NAVIGATION_TIMEOUT_S,
    WEATHER_KMA_WEB_PARSE_TIMEOUT_S,
    WEATHER_KMA_WEB_PAGE_URL,
    WEATHER_KMA_WEB_REQUEST_INTERVAL_S,
    WEATHER_KMA_WEB_SELECTOR_TIMEOUT_S,
    WEATHER_KMA_WEB_USER_AGENT,
)

from .location import (
    KmaLocation,
    KmaLocationError,
    KmaWebLocationResolver,
    validate_kma_redirect_url,
    validate_kma_response_url,
    validate_kma_url,
)
from .models import DailyWeather
from .parser import (
    KmaWebParseError,
    KmaWebParseResult,
    PARSER_VERSION,
    parse_kma_web_html,
)
from .provider import WeatherProviderError


LOGGER = logging.getLogger(__name__)
_FRAGMENT_PATH = "/w/wnuri-fct2021/main/digital-forecast.do"
_PAGE_PATH = "/w/forecast/overall/short-term.do"
_ACCESS_DENIAL_CODES = {401, 403, 429}
_NO_BROWSER_FALLBACK_CODES = {
    "KMA_WEB_ACCESS_DENIED",
    "KMA_WEB_REDIRECT_REJECTED",
    "KMA_WEB_LOCATION_NOT_FOUND",
    "KMA_WEB_FORECAST_NOT_AVAILABLE",
    "KMA_WEB_TIMEOUT",
    "KMA_WEB_SOURCE_FAILED",
}
_ACCESS_DENIAL_MARKERS = (
    "access denied",
    "forbidden",
    "captcha",
    "robot check",
    "자동화된 접근",
    "접근이 거부",
    "접근이 제한",
    "서비스 이용이 제한",
)


class KmaWebBrowserError(RuntimeError):
    """Browser fallback error with a stable provider code."""

    def __init__(self, code: str, message: str | None = None, *, phase: str | None = None) -> None:
        self.code = code
        self.phase = phase
        super().__init__(message or code)


@dataclass(frozen=True)
class BrowserFetchResult:
    html: str
    source_url: str
    http_status: int


class KmaWebBrowser(Protocol):
    async def fetch(self, location: KmaLocation) -> BrowserFetchResult:
        ...


class _SingleFlight:
    """Share one provider task among identical coordinate/date requests."""

    def __init__(self) -> None:
        self._lock = LoopLocalAsyncLock()
        self._tasks: dict[str, asyncio.Task[list[DailyWeather]]] = {}
        self.owners = 0
        self.waiters = 0

    async def _remove(self, key: str, task: asyncio.Task[list[DailyWeather]]) -> None:
        async with self._lock:
            if self._tasks.get(key) is task:
                self._tasks.pop(key, None)

    async def run(
        self,
        key: str,
        factory: Callable[[], Awaitable[list[DailyWeather]]],
    ) -> list[DailyWeather]:
        async with self._lock:
            task = self._tasks.get(key)
            if task is None:
                task = asyncio.create_task(factory())
                self._tasks[key] = task
                self.owners += 1
                task.add_done_callback(
                    lambda completed: asyncio.create_task(self._remove(key, completed))
                )
            else:
                self.waiters += 1
        return await asyncio.shield(task)


class KmaWebBrowserClient:
    """Read the normal public KMA page's visible DOM with a pooled browser."""

    def __init__(
        self,
        *,
        page_url: str = WEATHER_KMA_WEB_PAGE_URL,
        navigation_timeout_s: float = WEATHER_KMA_WEB_NAVIGATION_TIMEOUT_S,
        selector_timeout_s: float = WEATHER_KMA_WEB_SELECTOR_TIMEOUT_S,
        request_interval_s: float = WEATHER_KMA_WEB_REQUEST_INTERVAL_S,
        max_response_bytes: int = WEATHER_KMA_WEB_MAX_RESPONSE_BYTES,
        headless: bool = WEATHER_KMA_WEB_BROWSER_HEADLESS,
        executable_path: str | None = None,
    ) -> None:
        self.page_url = _validate_fixed_url(page_url, _PAGE_PATH)
        self.navigation_timeout_s = max(0.1, float(navigation_timeout_s))
        self.selector_timeout_s = max(0.1, float(selector_timeout_s))
        self.request_interval_s = max(0.0, float(request_interval_s))
        self.max_response_bytes = max(1_024, int(max_response_bytes))
        self.headless = headless
        self.executable_path = executable_path or os.getenv(
            "KTO_WEATHER_WEB_BROWSER_EXECUTABLE_PATH"
        )
        self._lock = LoopLocalAsyncLock()
        self._next_request_at = 0.0
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self.browser_launch_count = 0
        self.last_request_events: list[dict[str, object]] = []
        self.blocked_external_urls: list[str] = []
        self.last_timings: dict[str, float] = {}

    async def _ensure_browser(self, async_playwright: Any) -> Any:
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return self._browser
            except Exception:  # noqa: BLE001 - reconnect below
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
            self._playwright = None
        self._playwright = await async_playwright().start()
        options: dict[str, object] = {"headless": self.headless}
        if self.executable_path:
            options["executable_path"] = self.executable_path
        self._browser = await self._playwright.chromium.launch(**options)
        self.browser_launch_count += 1
        return self._browser

    async def _wait_for_slot(self) -> None:
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_request_at = time.monotonic() + self.request_interval_s

    @staticmethod
    async def _safe_close(target: Any, timeout_s: float = 5.0) -> None:
        if target is None:
            return
        try:
            await asyncio.wait_for(target.close(), timeout=timeout_s)
        except Exception as error:  # noqa: BLE001 - cleanup must not mask result
            LOGGER.warning("Could not close KMA weather browser resource: %s", error)

    def _attach_network_audit(self, page: Any, denied: list[int]) -> None:
        def on_request(request: Any) -> None:
            url = str(request.url)
            self.last_request_events.append(
                {"kind": "request", "method": request.method, "url": url}
            )

        def on_response(response: Any) -> None:
            status = int(response.status)
            url = str(response.url)
            parsed = urlsplit(url)
            if (
                (parsed.hostname or "").casefold() in WEATHER_KMA_WEB_ALLOWED_HOSTS
                and status in _ACCESS_DENIAL_CODES
            ):
                denied.append(status)
            self.last_request_events.append(
                {"kind": "response", "status": status, "url": url}
            )

        page.on("request", on_request)
        page.on("response", on_response)

    async def _route_public_only(self, route: Any) -> None:
        url = str(route.request.url)
        parsed = urlsplit(url)
        if parsed.scheme in {"data", "blob", "about"}:
            await route.continue_()
            return
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (
            parsed.scheme == "https"
            and port in {None, 443}
            and (
                (parsed.hostname or "").casefold() in WEATHER_KMA_WEB_ALLOWED_HOSTS
                or (parsed.hostname or "").casefold() in WEATHER_KMA_WEB_BROWSER_DEPENDENCY_HOSTS
            )
        ):
            await route.continue_()
            return
        self.blocked_external_urls.append(url)
        await route.abort()

    async def fetch(self, location: KmaLocation) -> BrowserFetchResult:
        """Load a fixed public page and return only its visible forecast root."""

        started = time.perf_counter()
        async with self._lock:
            # Keep diagnostics bounded across the reusable browser lifetime.
            self.last_request_events = []
            self.blocked_external_urls = []
            try:
                from playwright.async_api import async_playwright
            except ImportError as error:
                raise KmaWebBrowserError("KMA_WEB_PARSE_FAILED", phase="browser_import") from error
            await self._wait_for_slot()
            browser = await self._ensure_browser(async_playwright)
            context = await browser.new_context(
                locale="ko-KR",
            )
            try:
                await context.route("**/*", self._route_public_only)
                page = await context.new_page()
            except Exception:
                await self._safe_close(context)
                raise
            denied: list[int] = []
            self._attach_network_audit(page, denied)
            page_url = (
                f"{self.page_url}#dong/{location.code}/"
                f"{location.lat:.6f}/{location.lng:.6f}"
            )
            try:
                try:
                    response = await page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=int(self.navigation_timeout_s * 1000),
                    )
                except Exception as error:  # Playwright has its own TimeoutError class
                    if self.blocked_external_urls:
                        raise KmaWebBrowserError(
                            "KMA_WEB_REDIRECT_REJECTED", phase="navigation"
                        ) from error
                    if error.__class__.__name__ == "TimeoutError":
                        raise KmaWebBrowserError(
                            "KMA_WEB_TIMEOUT", phase="navigation"
                        ) from error
                    raise KmaWebBrowserError(
                        "KMA_WEB_SOURCE_FAILED", phase="navigation"
                    ) from error
                if response is None:
                    raise KmaWebBrowserError("KMA_WEB_SOURCE_FAILED", phase="navigation")
                try:
                    validate_kma_response_url(response, expected_path=_PAGE_PATH)
                except Exception as error:
                    raise KmaWebBrowserError(
                        "KMA_WEB_REDIRECT_REJECTED", phase="navigation"
                    ) from error
                if response.status in _ACCESS_DENIAL_CODES or denied:
                    raise KmaWebBrowserError("KMA_WEB_ACCESS_DENIED", phase="navigation")
                if response.status >= 400:
                    raise KmaWebBrowserError("KMA_WEB_SOURCE_FAILED", phase="navigation")
                try:
                    body_text = await page.locator("body").inner_text(timeout=2_000)
                except Exception:
                    body_text = ""
                if _looks_like_access_denial(body_text):
                    raise KmaWebBrowserError("KMA_WEB_ACCESS_DENIED", phase="visible_dom")

                root = page.locator("#digital-forecast")
                if await root.count() == 0:
                    raise KmaWebBrowserError("KMA_WEB_PAGE_CHANGED", phase="selector")
                try:
                    await root.locator('.dfs-daily-slide[data-date]').first.wait_for(
                        state="visible",
                        timeout=int(self.selector_timeout_s * 1000),
                    )
                except Exception as error:
                    if denied:
                        raise KmaWebBrowserError("KMA_WEB_ACCESS_DENIED", phase="selector") from error
                    raise KmaWebBrowserError("KMA_WEB_TIMEOUT", phase="selector") from error

                # The default public screen is a 3-hour view and intentionally
                # shows qualitative wind labels.  Clicking the visible 1-hour
                # tab is the normal user action that exposes numeric m/s.
                one_hour = root.locator("a.tab-btn", has_text="1시간 간격")
                if await one_hour.count() and "on" not in (await one_hour.first.get_attribute("class") or ""):
                    await one_hour.first.click()
                    try:
                        await root.locator(".wspd:not(.qwsd)").first.wait_for(
                            state="visible",
                            timeout=int(self.selector_timeout_s * 1000),
                        )
                    except Exception as error:
                        if denied:
                            raise KmaWebBrowserError("KMA_WEB_ACCESS_DENIED", phase="selector") from error
                        raise KmaWebBrowserError("KMA_WEB_TIMEOUT", phase="selector") from error
                if denied:
                    raise KmaWebBrowserError("KMA_WEB_ACCESS_DENIED", phase="visible_dom")
                html = await root.evaluate("(node) => node.outerHTML")
                if not isinstance(html, str) or not html.strip():
                    raise KmaWebBrowserError("KMA_WEB_PAGE_CHANGED", phase="visible_dom")
                if len(html.encode("utf-8")) > self.max_response_bytes:
                    raise KmaWebBrowserError("KMA_WEB_PAGE_CHANGED", phase="visible_dom")
                current_url = urlsplit(page.url)
                current_url_without_fragment = urlunsplit(
                    (
                        current_url.scheme,
                        current_url.netloc,
                        current_url.path,
                        current_url.query,
                        "",
                    )
                )
                try:
                    validate_kma_redirect_url(
                        current_url_without_fragment,
                        base_url=page.url,
                        expected_path=_PAGE_PATH,
                    )
                except KmaLocationError as error:
                    raise KmaWebBrowserError(
                        "KMA_WEB_REDIRECT_REJECTED", phase="visible_dom"
                    ) from error
                self.last_timings = {
                    "browser_total_ms": round((time.perf_counter() - started) * 1000, 2)
                }
                return BrowserFetchResult(html, page.url, int(response.status))
            finally:
                await self._safe_close(page)
                await self._safe_close(context)

    async def close(self) -> None:
        async with self._lock:
            browser, self._browser = self._browser, None
            playwright, self._playwright = self._playwright, None
            await self._safe_close(browser)
            if playwright is not None:
                try:
                    await asyncio.wait_for(playwright.stop(), timeout=5.0)
                except Exception as error:  # noqa: BLE001 - best effort shutdown
                    LOGGER.warning("Could not stop KMA Playwright cleanly: %s", error)


def _validate_fixed_url(value: str, expected_path: str) -> str:
    parsed = urlsplit(value)
    validated = validate_kma_url(
        value,
        allowed_paths=frozenset({expected_path}),
    )
    if parsed.query or parsed.fragment:
        raise ValueError("KMA base source URL must not contain query or fragment data")
    return validated


def _map_parse_error(error: KmaWebParseError) -> WeatherProviderError:
    return WeatherProviderError(error.code)


def _looks_like_access_denial(html: str) -> bool:
    lowered = html.casefold()
    return any(marker.casefold() in lowered for marker in _ACCESS_DENIAL_MARKERS)


class KmaWebWeatherProvider:
    """Public KMA HTML provider with a strict HTTP → visible-DOM fallback."""

    name = "kma_web"
    max_forecast_days = 10
    parser_version = PARSER_VERSION

    def __init__(
        self,
        *,
        fragment_url: str = WEATHER_KMA_WEB_FRAGMENT_URL,
        page_url: str = WEATHER_KMA_WEB_PAGE_URL,
        timeout_s: float = WEATHER_KMA_WEB_HTTP_TIMEOUT_S,
        connect_timeout_s: float = WEATHER_KMA_WEB_CONNECT_TIMEOUT_S,
        parse_timeout_s: float = WEATHER_KMA_WEB_PARSE_TIMEOUT_S,
        request_interval_s: float = WEATHER_KMA_WEB_REQUEST_INTERVAL_S,
        max_response_bytes: int = WEATHER_KMA_WEB_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
        location_resolver: KmaWebLocationResolver | None = None,
        browser_client: KmaWebBrowser | None = None,
        browser_fallback_enabled: bool = True,
    ) -> None:
        self.fragment_url = _validate_fixed_url(fragment_url, _FRAGMENT_PATH)
        self.page_url = _validate_fixed_url(page_url, _PAGE_PATH)
        self.timeout = httpx.Timeout(
            max(0.1, float(timeout_s)),
            connect=max(0.1, min(float(timeout_s), float(connect_timeout_s))),
        )
        self.parse_timeout_s = max(0.1, float(parse_timeout_s))
        self.request_interval_s = max(0.0, float(request_interval_s))
        self.max_response_bytes = max(1_024, int(max_response_bytes))
        self.transport = transport
        self.location_resolver = location_resolver or KmaWebLocationResolver(
            timeout_s=timeout_s,
            connect_timeout_s=connect_timeout_s,
            transport=transport,
            request_interval_s=request_interval_s,
        )
        self._owns_location_resolver = location_resolver is None
        self.browser_client = (
            (
                browser_client
                or KmaWebBrowserClient(
                    page_url=self.page_url,
                    navigation_timeout_s=WEATHER_KMA_WEB_NAVIGATION_TIMEOUT_S,
                    selector_timeout_s=WEATHER_KMA_WEB_SELECTOR_TIMEOUT_S,
                    request_interval_s=request_interval_s,
                    max_response_bytes=max_response_bytes,
                )
            )
            if browser_fallback_enabled
            else None
        )
        self._owns_browser_client = browser_client is None and self.browser_client is not None
        self._http_client: httpx.AsyncClient | None = None
        self._http_client_lock = LoopLocalAsyncLock()
        self._request_lock = LoopLocalAsyncLock()
        self._next_request_at = 0.0
        self._singleflight = _SingleFlight()
        self.http_fetch_count = 0
        self.browser_fallback_count = 0
        self.last_diagnostics: dict[str, object] = {
            "provider": self.name,
            "parser_version": self.parser_version,
        }

    async def _http_client_or_create(self) -> httpx.AsyncClient:
        async with self._http_client_lock:
            if self._http_client is None:
                self._http_client = httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=False,
                    headers={
                        "User-Agent": WEATHER_KMA_WEB_USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "ko-KR,ko;q=0.9",
                    },
                    transport=self.transport,
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                )
            return self._http_client

    async def _wait_for_slot(self) -> None:
        async with self._request_lock:
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = time.monotonic() + self.request_interval_s

    @staticmethod
    def _flight_key(lat: float, lng: float, start_date: date, end_date: date) -> str:
        return f"{round(lat, 5):.5f}|{round(lng, 5):.5f}|{start_date}|{end_date}"

    @staticmethod
    def _with_location(days: list[DailyWeather], location: KmaLocation) -> list[DailyWeather]:
        return [day.model_copy(update={"location_name": location.name}) for day in days]

    async def _fetch_http(
        self,
        location: KmaLocation,
        *,
        fetched_at: datetime,
    ) -> tuple[KmaWebParseResult, str, int]:
        await self._wait_for_slot()
        client = await self._http_client_or_create()
        try:
            request_url = self.fragment_url
            request_params: dict[str, object] | None = {
                "code": location.code,
                "unit": "m/s",
                "hr1": "Y",
                "lat": f"{location.lat:.6f}",
                "lon": f"{location.lng:.6f}",
            }
            for _ in range(3):
                response = await client.get(request_url, params=request_params)
                if 300 <= response.status_code < 400:
                    redirect = response.headers.get("location")
                    if not redirect:
                        raise WeatherProviderError("KMA_WEB_REDIRECT_REJECTED")
                    request_url = validate_kma_redirect_url(
                        redirect,
                        base_url=str(response.url),
                        expected_path=_FRAGMENT_PATH,
                    )
                    request_params = None
                    continue
                break
            else:
                raise WeatherProviderError("KMA_WEB_REDIRECT_REJECTED")
        except KmaLocationError as error:
            raise WeatherProviderError(error.code) from error
        except (httpx.TimeoutException, TimeoutError) as error:
            self.last_diagnostics.update({"timeout_phase": "http"})
            raise WeatherProviderError("KMA_WEB_TIMEOUT", phase="http") from error
        except httpx.HTTPError as error:
            raise WeatherProviderError("KMA_WEB_SOURCE_FAILED") from error
        self.http_fetch_count += 1
        try:
            validate_kma_response_url(response, expected_path=_FRAGMENT_PATH)
        except KmaLocationError as error:
            raise WeatherProviderError(error.code) from error
        if response.status_code in _ACCESS_DENIAL_CODES:
            raise WeatherProviderError("KMA_WEB_ACCESS_DENIED")
        if response.status_code >= 400:
            raise WeatherProviderError("KMA_WEB_SOURCE_FAILED")
        content_type = response.headers.get("content-type", "").casefold()
        if "html" not in content_type:
            raise WeatherProviderError("KMA_WEB_PAGE_CHANGED")
        if len(response.content) > self.max_response_bytes:
            raise WeatherProviderError("KMA_WEB_PAGE_CHANGED")
        try:
            html = response.text
            if _looks_like_access_denial(html):
                raise WeatherProviderError("KMA_WEB_ACCESS_DENIED")
            parsed = await self._parse_html(
                html,
                source_url=str(response.url),
                fetched_at=fetched_at,
                expected_location_code=location.code,
            )
        except (UnicodeError, KmaWebParseError) as error:
            if isinstance(error, UnicodeError):
                raise WeatherProviderError("KMA_WEB_PARSE_FAILED") from error
            raise _map_parse_error(error) from error
        return parsed, str(response.url), response.status_code

    async def _parse_html(
        self,
        html: str,
        *,
        source_url: str,
        fetched_at: datetime,
        expected_location_code: str,
    ) -> KmaWebParseResult:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    parse_kma_web_html,
                    html,
                    source=self.name,
                    source_url=source_url,
                    fetched_at=fetched_at,
                    expected_location_code=expected_location_code,
                    parser_version=self.parser_version,
                ),
                timeout=self.parse_timeout_s,
            )
        except asyncio.TimeoutError as error:
            self.last_diagnostics.update({"timeout_phase": "parse"})
            raise WeatherProviderError("KMA_WEB_TIMEOUT", phase="parse") from error

    @staticmethod
    def _select_days(
        parsed: KmaWebParseResult,
        *,
        start_date: date,
        end_date: date,
    ) -> list[DailyWeather]:
        selected = [day for day in parsed.days if start_date <= day.date <= end_date]
        if not selected:
            raise WeatherProviderError("KMA_WEB_FORECAST_NOT_AVAILABLE")
        return selected

    @staticmethod
    def _should_try_browser(error: WeatherProviderError) -> bool:
        return error.code not in _NO_BROWSER_FALLBACK_CODES

    async def _daily_forecast_once(
        self,
        lat: float,
        lng: float,
        start_date: date,
        end_date: date,
    ) -> list[DailyWeather]:
        try:
            location = await self.location_resolver.resolve(lat, lng)
        except KmaLocationError as error:
            self.last_diagnostics = {
                "provider": self.name,
                "parser_version": self.parser_version,
                "location_error": error.code,
            }
            raise WeatherProviderError(error.code) from error

        fetched_at = datetime.now(timezone.utc)

        diagnostics: dict[str, object] = {
            "provider": self.name,
            "requested_url": self.fragment_url,
            "location_url": getattr(self.location_resolver, "source_url", None),
            "location_code": location.code,
            "location_name": location.name,
            "parser_version": self.parser_version,
            "fallback_used": False,
        }
        try:
            parsed, final_url, http_status = await self._fetch_http(
                location,
                fetched_at=fetched_at,
            )
            diagnostics.update(
                {
                    "final_url": final_url,
                    "http_status": http_status,
                    "parser_path": "http_html",
                    "field_count": parsed.field_count,
                    "content_fingerprint": parsed.content_fingerprint,
                }
            )
        except WeatherProviderError as http_error:
            diagnostics.update({"http_error": http_error.code})
            if http_error.phase:
                diagnostics.update({"timeout_phase": http_error.phase})
            if not self._should_try_browser(http_error) or self.browser_client is None:
                self.last_diagnostics = diagnostics
                raise
            self.browser_fallback_count += 1
            try:
                browser_result = await self.browser_client.fetch(location)
                browser_url = urlsplit(browser_result.source_url)
                browser_url_without_fragment = urlunsplit(
                    (
                        browser_url.scheme,
                        browser_url.netloc,
                        browser_url.path,
                        browser_url.query,
                        "",
                    )
                )
                try:
                    validate_kma_redirect_url(
                        browser_url_without_fragment,
                        base_url=browser_result.source_url,
                        expected_path=_PAGE_PATH,
                    )
                except KmaLocationError as error:
                    raise WeatherProviderError("KMA_WEB_REDIRECT_REJECTED") from error
                if browser_result.http_status in _ACCESS_DENIAL_CODES:
                    raise WeatherProviderError("KMA_WEB_ACCESS_DENIED")
                if browser_result.http_status >= 400:
                    raise WeatherProviderError("KMA_WEB_SOURCE_FAILED")
                if len(browser_result.html.encode("utf-8")) > self.max_response_bytes:
                    raise WeatherProviderError("KMA_WEB_PAGE_CHANGED")
                browser_fetched_at = datetime.now(timezone.utc)
                parsed = await self._parse_html(
                    browser_result.html,
                    source_url=browser_result.source_url,
                    fetched_at=browser_fetched_at,
                    expected_location_code=location.code,
                )
                parsed = replace(parsed, parser_path="visible_dom")
                diagnostics.update(
                    {
                        "final_url": browser_result.source_url,
                        "http_status": browser_result.http_status,
                        "parser_path": "visible_dom",
                        "field_count": parsed.field_count,
                        "content_fingerprint": parsed.content_fingerprint,
                        "fallback_used": True,
                    }
                )
            except KmaWebParseError as error:
                diagnostics.update({"browser_error": error.code})
                self.last_diagnostics = diagnostics
                raise WeatherProviderError(error.code) from error
            except KmaWebBrowserError as error:
                diagnostics.update(
                    {
                        "browser_error": error.code,
                        "timeout_phase": error.phase,
                    }
                )
                self.last_diagnostics = diagnostics
                raise WeatherProviderError(error.code) from error
            except WeatherProviderError as error:
                diagnostics.update(
                    {
                        "browser_error": error.code,
                        "timeout_phase": error.phase,
                    }
                )
                self.last_diagnostics = diagnostics
                raise
            except Exception as error:  # noqa: BLE001 - browser must fail safe
                diagnostics.update({"browser_error": "KMA_WEB_SOURCE_FAILED"})
                self.last_diagnostics = diagnostics
                raise WeatherProviderError("KMA_WEB_SOURCE_FAILED") from error

        self.last_diagnostics = diagnostics
        return self._with_location(
            self._select_days(parsed, start_date=start_date, end_date=end_date),
            location,
        )

    async def daily_forecast(
        self,
        lat: float,
        lng: float,
        start_date: date,
        end_date: date,
    ) -> list[DailyWeather]:
        if end_date < start_date:
            raise WeatherProviderError("KMA_WEB_FORECAST_NOT_AVAILABLE")
        key = self._flight_key(lat, lng, start_date, end_date)
        return await self._singleflight.run(
            key,
            lambda: self._daily_forecast_once(lat, lng, start_date, end_date),
        )

    async def close(self) -> None:
        async with self._http_client_lock:
            client, self._http_client = self._http_client, None
            if client is not None:
                await client.aclose()
        if self._owns_location_resolver:
            await self.location_resolver.close()
        if self._owns_browser_client and self.browser_client is not None:
            close = getattr(self.browser_client, "close", None)
            if close is not None:
                await close()


# Name aliases make the HTTP/browser boundary discoverable to callers that
# prefer an explicit client naming convention.
HttpKmaWebWeatherProvider = KmaWebWeatherProvider
BrowserKmaWebClient = KmaWebBrowserClient


__all__ = [
    "BrowserFetchResult",
    "BrowserKmaWebClient",
    "HttpKmaWebWeatherProvider",
    "KmaWebBrowserClient",
    "KmaWebWeatherProvider",
]
