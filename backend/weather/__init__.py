"""Future weather contracts, provider abstraction, and cache."""

from .models import (
    DailyWeather,
    WeatherForecastRequest,
    WeatherForecastResponse,
    WeatherStatus,
)
from .provider import KmaWeatherProvider, UnavailableWeatherProvider, WeatherProvider
from .repository import WeatherCache
from .service import WeatherService

__all__ = [
    "DailyWeather",
    "KmaWeatherProvider",
    "UnavailableWeatherProvider",
    "WeatherCache",
    "WeatherForecastRequest",
    "WeatherForecastResponse",
    "WeatherProvider",
    "WeatherService",
    "WeatherStatus",
]
