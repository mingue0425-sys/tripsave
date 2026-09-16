"""FastAPI router for multi-stop planning."""

from fastapi import APIRouter, HTTPException

from .models import OptimizedRoute, OptimizeRouteRequest
from .service import ItineraryService, ItineraryServiceUnavailable


def create_router(service: ItineraryService) -> APIRouter:
    router = APIRouter(prefix="/api/routes", tags=["itinerary"])

    @router.post("/optimize", response_model=OptimizedRoute)
    async def optimize_route(request: OptimizeRouteRequest) -> OptimizedRoute:
        try:
            return await service.optimize(request)
        except ItineraryServiceUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": error.code,
                    "message": "로컬 경로 엔진을 사용할 수 없어 경유지 경로를 계산할 수 없습니다.",
                },
            ) from error

    return router
