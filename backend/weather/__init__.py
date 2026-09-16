"""Future weather contracts, provider abstraction, and cache."""

from .models import (
    DailyWeather,
    WeatherForecastRequest,
    WeatherForecastResponse,
    WeatherStatus,
)
from .kma_web import KmaWebBrowserClient, KmaWebWeatherProvider
from .location import KmaLocation, KmaWebLocationResolver
from .provider import (
    KmaApiWeatherProvider,
    KmaWeatherProvider,
    UnavailableWeatherProvider,
    WeatherProvider,
)
from .repository import WeatherCache
from .service import WeatherService

__all__ = [
    "DailyWeather",
    "KmaApiWeatherProvider",
    "KmaLocation",
    "KmaWebBrowserClient",
    "KmaWebLocationResolver",
    "KmaWebWeatherProvider",
    "KmaWeatherProvider",
    "UnavailableWeatherProvider",
    "WeatherCache",
    "WeatherForecastRequest",
    "WeatherForecastResponse",
    "WeatherProvider",
    "WeatherService",
    "WeatherStatus",
]
