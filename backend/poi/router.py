"""FastAPI router for local POI queries."""

from fastapi import APIRouter

from .models import PoiSearchRequest, PoiSearchResponse
from .service import PoiService


def create_router(service: PoiService) -> APIRouter:
    router = APIRouter(prefix="/api/poi", tags=["poi"])

    @router.post("/search", response_model=PoiSearchResponse)
    async def search_poi(request: PoiSearchRequest) -> PoiSearchResponse:
        return await service.search(request)

    @router.get("/status")
    async def poi_status() -> dict[str, object]:
        return await service.status()

    return router
