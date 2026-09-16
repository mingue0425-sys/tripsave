"""FastAPI router for future forecast context."""

from fastapi import APIRouter

from .models import WeatherForecastRequest, WeatherForecastResponse
from .service import WeatherService


def create_router(service: WeatherService) -> APIRouter:
    router = APIRouter(prefix="/api/weather", tags=["weather"])

    @router.post("/forecast", response_model=WeatherForecastResponse)
    async def weather_forecast(request: WeatherForecastRequest) -> WeatherForecastResponse:
        return await service.forecast(request)

    @router.get("/status")
    async def weather_status() -> dict[str, object]:
        return await service.status()

    return router
