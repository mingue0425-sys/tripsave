"""FastAPI entry point for Korea Trip Optimizer V0.5."""

import logging

from fastapi import FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import (
    APP_VERSION,
    DRIVING_COST_API_URL,
    FUEL_API_URL,
    FUEL_STATUS_URL,
    OSRM_BASE_URL,
    OSRM_CONNECT_TIMEOUT_S,
    OSRM_REQUEST_TIMEOUT_S,
    ROUTE_API_URL,
    STATIC_DIR,
    TOLL_API_URL,
    TOLL_DEBUG_MODE,
    TOLL_INDEX_DB,
    TOLL_STATUS_URL,
    TEMPLATES_DIR,
    map_config,
)
from backend.places import search_places
from backend.accommodation.models import (
    AccommodationSearchRequest,
    AccommodationSearchResponse,
)
from backend.accommodation.service import ACCOMMODATION_API_URL, AccommodationService
from backend.fuel.errors import FuelServiceError
from backend.fuel.models import (
    DrivingCostRequest,
    DrivingCostResponse,
    FuelCalculationRequest,
    FuelResponse,
)
from backend.fuel.service import DrivingCostService, FuelPriceService, make_fuel_response
from backend.routing.errors import (
    InvalidRouteInputError,
    NoRouteError,
    NoSegmentError,
    RoutingEngineUnavailableError,
    RoutingError,
    RoutingTimeoutError,
)
from backend.routing.models import RouteRequest, RouteResponse
from backend.routing.osrm import OSRMClient
from backend.tolls.errors import TollServiceError
from backend.tolls.models import TollCalculationRequest, TollResponse
from backend.tolls.service import TollCalculator


LOGGER = logging.getLogger(__name__)


app = FastAPI(title="Korea Trip Optimizer", version=APP_VERSION)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
routing_client = OSRMClient(
    base_url=OSRM_BASE_URL,
    request_timeout_s=OSRM_REQUEST_TIMEOUT_S,
    connect_timeout_s=OSRM_CONNECT_TIMEOUT_S,
)
toll_calculator = TollCalculator(index_path=TOLL_INDEX_DB)
fuel_price_service = FuelPriceService(database_path=TOLL_INDEX_DB)
driving_cost_service = DrivingCostService(
    toll_calculator=toll_calculator,
    fuel_service=fuel_price_service,
    routing_client=routing_client,
)
accommodation_service = AccommodationService(database_path=TOLL_INDEX_DB)

# StaticFiles performs safe path handling. Basemap tiles are intentionally
# fetched from the configured OpenFreeMap provider during development; this
# mount serves only application-owned assets and the small search dataset.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Keep route validation failures in the same public error envelope."""

    if request.url.path == ROUTE_API_URL:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "출발지와 목적지 좌표가 유효하지 않습니다.",
                },
            },
        )
    if request.url.path == TOLL_API_URL:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "통행료 계산 요청이 유효하지 않습니다.",
                },
            },
        )
    if request.url.path in {FUEL_API_URL, DRIVING_COST_API_URL}:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "유류비 계산 요청이 유효하지 않습니다.",
                },
            },
        )
    if request.url.path == ACCOMMODATION_API_URL:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "숙소 검색 요청이 올바르지 않습니다.",
                },
            },
        )
    return await request_validation_exception_handler(request, exc)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the map shell and current basemap provider configuration."""

    current_map_config = map_config()
    default_basemap = current_map_config["basemaps"][
        current_map_config["defaultBasemap"]
    ]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_version": APP_VERSION,
            "map_config": current_map_config,
            "basemap": default_basemap,
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    """Return a small liveness response for local development and tests."""

    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/places/search")
async def place_search(
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=8, ge=1, le=8),
) -> dict[str, object]:
    """Search the small local city index served by this application."""

    query = q.strip()
    return {
        "query": query,
        "results": [place.model_dump() for place in search_places(query, limit)],
    }


@app.post(
    ACCOMMODATION_API_URL,
    response_model=AccommodationSearchResponse,
)
async def search_accommodations(
    request: AccommodationSearchRequest,
) -> AccommodationSearchResponse:
    """Search public accommodation pages for one destination and stay."""

    return await accommodation_service.search(request)


def routing_error_response(error: RoutingError) -> JSONResponse:
    return JSONResponse(
        status_code=error.http_status,
        content={
            "status": "error",
            "error": {
                "code": error.code,
                "message": error.message,
            },
        },
    )


@app.get("/api/routing/status")
async def routing_status() -> JSONResponse:
    """Report whether the configured local OSRM server is ready."""

    status = await routing_client.status()
    http_status = 200 if status["status"] == "ready" else 503
    return JSONResponse(status_code=http_status, content=status)


@app.post(
    "/api/routes",
    response_model=RouteResponse,
    response_model_exclude_none=True,
)
async def create_route(request: RouteRequest) -> RouteResponse | JSONResponse:
    """Calculate one validated car route through the local OSRM server."""

    try:
        route = await routing_client.route(request.origin, request.destination)
    except InvalidRouteInputError as error:
        return routing_error_response(error)
    except NoSegmentError as error:
        return routing_error_response(error)
    except NoRouteError as error:
        return routing_error_response(error)
    except RoutingEngineUnavailableError as error:
        return routing_error_response(error)
    except RoutingTimeoutError as error:
        return routing_error_response(error)
    except RoutingError as error:
        LOGGER.exception("Unhandled routing failure: %s", error)
        return routing_error_response(error)

    return RouteResponse(status="ok", route=route)


def toll_error_response(error: TollServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=error.http_status,
        content={
            "status": "error",
            "error": {"code": error.code, "message": error.message},
        },
    )


@app.get(TOLL_STATUS_URL)
async def toll_status() -> JSONResponse:
    """Report local OSM toll-index readiness and configured source policy."""

    status = await toll_calculator.status()
    http_status = 200 if status["status"] == "ready" else 503
    return JSONResponse(status_code=http_status, content=status)


@app.post(TOLL_API_URL, response_model=TollResponse, response_model_exclude_none=True)
async def calculate_tolls(
    request: TollCalculationRequest,
) -> TollResponse | JSONResponse:
    """Match a canonical route and query the fixed official HTML source."""

    try:
        return await toll_calculator.calculate(request)
    except TollServiceError as error:
        if error.http_status >= 500:
            LOGGER.warning("Toll calculation service failure: %s", error)
        return toll_error_response(error)


def fuel_error_response(error: FuelServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=error.http_status,
        content={
            "status": "error",
            "error": {"code": error.code, "message": error.message},
        },
    )


@app.get(FUEL_STATUS_URL)
async def fuel_status() -> JSONResponse:
    """Report the public fuel source and local cache policy."""

    return JSONResponse(status_code=200, content=await fuel_price_service.status())


@app.post(FUEL_API_URL, response_model=FuelResponse, response_model_exclude_none=True)
async def calculate_fuel(
    request: FuelCalculationRequest,
) -> FuelResponse | JSONResponse:
    """Calculate fuel cost from the already calculated canonical route."""

    try:
        result = await fuel_price_service.calculate(request)
        return make_fuel_response(result)
    except FuelServiceError as error:
        return fuel_error_response(error)


@app.post(
    DRIVING_COST_API_URL,
    response_model=DrivingCostResponse,
    response_model_exclude_none=True,
)
async def calculate_driving_cost(
    request: DrivingCostRequest,
) -> DrivingCostResponse | JSONResponse:
    """Return route-bound toll, fuel, and one-way/return driving costs."""

    try:
        return await driving_cost_service.calculate(request)
    except FuelServiceError as error:
        return fuel_error_response(error)
    except TollServiceError as error:
        return toll_error_response(error)


@app.get("/api/tolls/debug/{route_id}")
async def toll_debug(route_id: str) -> JSONResponse:
    """Return the latest stage evidence for a route in development mode."""

    if not TOLL_DEBUG_MODE:
        return JSONResponse(status_code=404, content={"status": "not_found"})
    value = toll_calculator.debug(route_id)
    if value is None:
        return JSONResponse(
            status_code=404,
            content={"status": "not_found", "message": "No toll diagnostic for route."},
        )
    return JSONResponse(status_code=200, content={"status": "ok", **value})
