"""FastAPI entry point for Korea Trip Optimizer V1.0.0."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.accommodation.models import (
    AccommodationSearchRequest,
    AccommodationSearchResponse,
)
from backend.accommodation.service import ACCOMMODATION_API_URL, AccommodationService
from backend.city_search import search_places
from backend.entities import EntityResolver, EntityResolveRequest, EntityResolveResponse
from backend.entities.repository import EntityResolutionRepository
from backend.fuel.errors import FuelServiceError
from backend.fuel.models import (
    DrivingCostRequest,
    DrivingCostResponse,
    FuelCalculationRequest,
    FuelResponse,
)
from backend.fuel.service import (
    DrivingCostService,
    FuelPriceService,
    make_fuel_response,
)
from backend.places.models import (
    PlaceCategory,
    PlaceDestination,
    PlaceSearchRequest,
    PlaceSearchResponse,
)
from backend.places.service import PLACES_API_URL, places_service
from backend.recommendations import (
    RankingResult,
    RecommendationDebugRankRequest,
    RecommendationError,
    RecommendationRankRequest,
    RecommendationService,
)
from backend.routing.errors import (
    InvalidRouteInputError,
    NoRouteError,
    NoSegmentError,
    RoutingEngineUnavailableError,
    RoutingError,
    RoutingTimeoutError,
)
from backend.routing.identity import make_route_id
from backend.routing.models import RouteRequest, RouteResponse
from backend.routing.osrm import OSRMClient
from backend.tolls.errors import TollServiceError
from backend.tolls.models import TollCalculationRequest, TollResponse
from backend.tolls.service import TollCalculator
from backend.trips import (
    SourceDataStatus,
    TripAssemblyError,
    TripCandidateRequest,
    TripCandidateResponse,
    TripCandidateService,
)
from backend.trips.candidate_sets import (
    CandidateSetCapacityError,
    CandidateSetError,
    CandidateSetStore,
)
from config import (
    ACCOMMODATION_CACHE_DB,
    APP_VERSION,
    CANDIDATE_SET_DB,
    CANDIDATE_SET_MAX_SETS,
    CANDIDATE_SET_TTL_S,
    CANONICAL_PLACES_API_URL,
    DEBUG_RECOMMENDATIONS_API_URL,
    DRIVING_COST_API_URL,
    ENTITY_RESOLUTION_DB,
    ENTITY_RESOLVE_API_URL,
    FUEL_API_URL,
    FUEL_STATUS_URL,
    OSRM_BASE_URL,
    OSRM_CONNECT_TIMEOUT_S,
    OSRM_REQUEST_TIMEOUT_S,
    RECOMMENDATIONS_API_URL,
    ROUTE_API_URL,
    STATIC_DIR,
    TEMPLATES_DIR,
    TOLL_API_URL,
    TOLL_DEBUG_MODE,
    TOLL_INDEX_DB,
    TOLL_STATUS_URL,
    TRIP_CANDIDATES_API_URL,
    map_config,
)
from crawler.accommodation.errors import AccommodationSourceError

LOGGER = logging.getLogger(__name__)


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
accommodation_service = AccommodationService(database_path=ACCOMMODATION_CACHE_DB)
entity_repository = EntityResolutionRepository(ENTITY_RESOLUTION_DB)
entity_resolver = EntityResolver(overrides=entity_repository.load_overrides())
trip_candidate_service = TripCandidateService()
recommendation_service = RecommendationService()
candidate_set_store = CandidateSetStore(
    CANDIDATE_SET_DB,
    ttl_s=CANDIDATE_SET_TTL_S,
    max_sets=CANDIDATE_SET_MAX_SETS,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Keep reusable clients alive, then close every feature on shutdown."""

    try:
        await toll_calculator.warmup()
    except Exception as error:  # noqa: BLE001 - browser warm-up must not block app startup
        # HTTP remains the primary source; a missing/unlaunchable browser must
        # not prevent the local route/cost service from starting.
        LOGGER.warning("Toll browser warm-up unavailable; fallback stays lazy: %s", error)
    try:
        yield
    finally:
        await accommodation_service.close()
        await places_service.close()
        await toll_calculator.close()
        candidate_set_store.close()
        entity_repository.close()


app = FastAPI(title="Korea Trip Optimizer", version=APP_VERSION, lifespan=lifespan)

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
    if request.url.path == PLACES_API_URL and request.method == "POST":
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "주변 장소 검색 요청이 올바르지 않습니다.",
                },
            },
        )
    if request.url.path == TRIP_CANDIDATES_API_URL and request.method == "POST":
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "여행 후보 생성 요청이 올바르지 않습니다.",
                },
            },
        )
    if request.url.path == RECOMMENDATIONS_API_URL and request.method == "POST":
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "추천 순위 생성 요청이 올바르지 않습니다.",
                },
            },
        )
    if request.url.path == DEBUG_RECOMMENDATIONS_API_URL and request.method == "POST":
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "디버그 추천 순위 생성 요청이 올바르지 않습니다.",
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
    PLACES_API_URL,
    response_model=PlaceSearchResponse,
)
async def search_places_around_destination(
    request: PlaceSearchRequest,
) -> PlaceSearchResponse:
    """Search public restaurant and attraction pages near the destination."""

    return await places_service.search(request)


@app.post(
    ACCOMMODATION_API_URL,
    response_model=AccommodationSearchResponse,
)
async def search_accommodations(
    request: AccommodationSearchRequest,
) -> AccommodationSearchResponse:
    """Search public accommodation pages for one destination and stay."""

    return await accommodation_service.search(request)


@app.post(
    ENTITY_RESOLVE_API_URL,
    response_model=EntityResolveResponse,
    response_model_exclude_none=True,
)
async def resolve_entities(request: EntityResolveRequest) -> EntityResolveResponse:
    """Resolve caller-supplied source records without fetching any URL."""

    def resolve_and_persist() -> EntityResolveResponse:
        result = entity_resolver.resolve(request.records, offers=request.offers)
        entity_repository.persist(result)
        return result

    return await asyncio.to_thread(resolve_and_persist)


def _place_category_status(
    response: PlaceSearchResponse,
    category: PlaceCategory,
) -> SourceDataStatus:
    results = [place for place in response.results if place.category == category]
    issues = [issue for issue in response.issues if issue.category == category]
    if issues and results:
        return SourceDataStatus.PARTIAL
    if issues:
        return SourceDataStatus.UNAVAILABLE
    return SourceDataStatus.OK if results else SourceDataStatus.EMPTY


def _accommodation_status(response: AccommodationSearchResponse) -> SourceDataStatus:
    if response.issues and response.results:
        return SourceDataStatus.PARTIAL
    if response.issues:
        return SourceDataStatus.UNAVAILABLE
    return SourceDataStatus.OK if response.results else SourceDataStatus.EMPTY


def _issue_codes(issues: list[object]) -> list[str]:
    return [str(getattr(issue, "code", "SOURCE_ERROR")) for issue in issues]


async def _load_trip_candidate_dependencies(
    request: TripCandidateRequest,
) -> tuple[
    object,
    object,
    list[object],
    list[object],
    dict[str, SourceDataStatus],
    list[str],
]:
    """Reuse existing services while keeping each source failure isolated."""

    route = request.route
    driving_cost = request.driving_cost
    statuses = dict(request.source_statuses)
    warnings = list(request.source_warnings)

    if route is None:
        try:
            route = await routing_client.route(request.origin, request.destination)
        except RoutingError as error:
            statuses.setdefault("driving", SourceDataStatus.UNAVAILABLE)
            warnings.append(error.code)
            LOGGER.warning("Trip candidate route unavailable: %s", error)
        except Exception:
            statuses.setdefault("driving", SourceDataStatus.UNAVAILABLE)
            warnings.append("ROUTE_UNAVAILABLE")
            LOGGER.exception("Trip candidate route lookup failed")
    if route is not None and route.route_id is None:
        route = route.model_copy(update={
            "route_id": make_route_id(request.origin, request.destination, route)
        })

    if driving_cost is None and route is not None:
        driving_request = DrivingCostRequest(
            route_id=route.route_id,
            origin=request.origin,
            destination=request.destination,
            route=route,
            fuel_type=request.vehicle.fuel_type,
            fuel_efficiency_km_per_l=request.vehicle.fuel_efficiency_km_per_l,
            vehicle_class=request.vehicle.vehicle_class,
            round_trip_mode=request.vehicle.round_trip_mode,
        )
        try:
            driving_response = await driving_cost_service.calculate(driving_request)
            driving_cost = driving_response.driving_cost
        except (FuelServiceError, TollServiceError) as error:
            statuses.setdefault("driving", SourceDataStatus.UNAVAILABLE)
            warnings.append(str(getattr(error, "code", "DRIVING_COST_UNAVAILABLE")))
            LOGGER.warning("Trip candidate driving cost unavailable: %s", error)
        except Exception:
            statuses.setdefault("driving", SourceDataStatus.UNAVAILABLE)
            warnings.append("DRIVING_COST_UNAVAILABLE")
            LOGGER.exception("Trip candidate driving-cost lookup failed")
    if "driving" not in statuses:
        statuses["driving"] = (
            SourceDataStatus.OK
            if driving_cost is not None and driving_cost.round_trip.complete
            else SourceDataStatus.PARTIAL
            if driving_cost is not None
            else SourceDataStatus.UNAVAILABLE
        )

    canonical_places: list[object] = []
    offers: list[object] = []
    has_supplied_place_data = any(
        value is not None
        for value in (
            request.canonical_places,
            request.accommodations,
            request.restaurants,
            request.attractions,
        )
    )
    if has_supplied_place_data:
        # The assembly service reads the category-specific fields directly.
        canonical_places = list(request.canonical_places or [])
        offers = list(request.offers or [])
    else:
        raw_records: list[object] = []
        accommodation_response: AccommodationSearchResponse | None = None
        places_response: PlaceSearchResponse | None = None
        nights = (request.end_date - request.start_date).days
        if nights > 0 and request.offers is None:
            try:
                accommodation_response = await accommodation_service.search(
                    AccommodationSearchRequest(
                        destination=request.destination,
                        checkin=request.start_date,
                        checkout=request.end_date,
                        adults=request.adults,
                        children=request.children,
                    )
                )
                statuses["accommodation"] = _accommodation_status(accommodation_response)
                warnings.extend(_issue_codes(accommodation_response.issues))
                for result in accommodation_response.results:
                    raw_records.append(result.place)
                    offers.extend(result.offers)
            except AccommodationSourceError as error:
                statuses["accommodation"] = SourceDataStatus.UNAVAILABLE
                warnings.append(str(getattr(error, "code", "ACCOMMODATION_SOURCE_FAILED")))
                LOGGER.warning("Trip candidate accommodation source failed: %s", error)
            except Exception:
                statuses["accommodation"] = SourceDataStatus.UNAVAILABLE
                warnings.append("ACCOMMODATION_SOURCE_UNAVAILABLE")
                LOGGER.exception("Trip candidate accommodation lookup failed")
        elif nights == 0:
            statuses.setdefault("accommodation", SourceDataStatus.NOT_REQUIRED)
        else:
            offers = list(request.offers or [])

        try:
            places_response = await places_service.search(
                PlaceSearchRequest(
                    destination=PlaceDestination(
                        lat=request.destination.lat,
                        lng=request.destination.lng,
                        label=request.destination.label or "destination",
                    ),
                    categories=[PlaceCategory.RESTAURANT, PlaceCategory.ATTRACTION],
                )
            )
            raw_records.extend(places_response.results)
            statuses["restaurants"] = _place_category_status(
                places_response, PlaceCategory.RESTAURANT
            )
            statuses["attractions"] = _place_category_status(
                places_response, PlaceCategory.ATTRACTION
            )
            warnings.extend(_issue_codes(places_response.issues))
        except Exception:
            statuses["restaurants"] = SourceDataStatus.UNAVAILABLE
            statuses["attractions"] = SourceDataStatus.UNAVAILABLE
            warnings.append("PLACES_SOURCE_UNAVAILABLE")
            LOGGER.exception("Trip candidate places lookup failed")

        if raw_records:
            try:
                resolved = await asyncio.to_thread(
                    entity_resolver.resolve,
                    raw_records,
                    offers=offers,
                )
                canonical_places = list(resolved.canonical_places)
            except Exception:
                warnings.append("ENTITY_RESOLUTION_UNAVAILABLE")
                LOGGER.exception("Trip candidate entity resolution failed")
        if "accommodation" not in statuses:
            statuses["accommodation"] = (
                SourceDataStatus.OK if offers else SourceDataStatus.UNAVAILABLE
            )
        statuses.setdefault("restaurants", SourceDataStatus.EMPTY)
        statuses.setdefault("attractions", SourceDataStatus.EMPTY)

    return route, driving_cost, canonical_places, offers, statuses, list(dict.fromkeys(warnings))


@app.post(
    TRIP_CANDIDATES_API_URL,
    response_model=TripCandidateResponse,
)
async def create_trip_candidates(
    request: TripCandidateRequest,
) -> TripCandidateResponse | JSONResponse:
    """Assemble one or more candidates from existing route/source services."""

    try:
        route, driving_cost, canonical_places, offers, statuses, warnings = (
            await _load_trip_candidate_dependencies(request)
        )
        assembly_request = request.model_copy(
            update={
                "route": route,
                "driving_cost": driving_cost,
                "canonical_places": canonical_places,
                "offers": offers,
                "source_statuses": statuses,
                "source_warnings": warnings,
            }
        )
        response = await asyncio.to_thread(trip_candidate_service.assemble, assembly_request)
        try:
            candidate_set = await asyncio.to_thread(candidate_set_store.save, response)
        except CandidateSetCapacityError as error:
            return JSONResponse(
                status_code=error.http_status,
                content={
                    "status": "error",
                    "error": {"code": error.code, "message": error.message},
                },
            )
        except Exception:
            LOGGER.exception("Candidate-set persistence failed")
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "error": {
                        "code": "CANDIDATE_SET_UNAVAILABLE",
                        "message": "여행 후보 집합을 저장하지 못했습니다.",
                    },
                },
            )
        return response.model_copy(update={"candidate_set_id": candidate_set.id})
    except TripAssemblyError as error:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {"code": "INVALID_CANDIDATE_DEPENDENCY", "message": str(error)},
            },
        )


@app.post(
    RECOMMENDATIONS_API_URL,
    response_model=RankingResult,
)
async def rank_recommendations(
    request: RecommendationRankRequest,
) -> RankingResult | JSONResponse:
    """Rank only server-persisted candidates addressed by set ID."""

    try:
        candidate_set = await asyncio.to_thread(
            candidate_set_store.load,
            request.candidate_set_id,
            request.candidate_ids,
            request_fingerprint=request.request_fingerprint,
        )
    except CandidateSetError as error:
        return JSONResponse(
            status_code=error.http_status,
            content={
                "status": "error",
                "error": {"code": error.code, "message": error.message},
            },
        )
    try:
        result = await asyncio.to_thread(
            recommendation_service.rank,
            candidate_set.candidates,
            request.mode,
            custom_weights=request.custom_weights,
            limit=request.limit,
        )
    except RecommendationError as error:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {"code": "INVALID_RECOMMENDATION_REQUEST", "message": str(error)},
            },
        )
    return result.model_copy(
        update={
            "candidate_set_id": candidate_set.id,
            "request_fingerprint": candidate_set.request_fingerprint,
        }
    )


async def debug_rank_recommendations(
    request: RecommendationDebugRankRequest,
) -> RankingResult | JSONResponse:
    """Rank caller-supplied full candidates only when KTO_DEBUG=1."""

    if not TOLL_DEBUG_MODE:
        return JSONResponse(status_code=404, content={"status": "not_found"})
    candidates = list(request.candidates)
    if request.candidate_ids:
        by_id = {candidate.id: candidate for candidate in candidates}
        candidates = [by_id[candidate_id] for candidate_id in request.candidate_ids]
    try:
        return await asyncio.to_thread(
            recommendation_service.rank,
            candidates,
            request.mode,
            custom_weights=request.custom_weights,
            limit=request.limit,
        )
    except RecommendationError as error:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {"code": "INVALID_RECOMMENDATION_REQUEST", "message": str(error)},
            },
        )


if TOLL_DEBUG_MODE:
    # Keep full-candidate ranking out of the production OpenAPI surface.
    app.add_api_route(
        DEBUG_RECOMMENDATIONS_API_URL,
        debug_rank_recommendations,
        methods=["POST"],
        response_model=RankingResult,
    )


@app.get(CANONICAL_PLACES_API_URL)
async def list_canonical_places(
    category: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=500, ge=1, le=5_000),
) -> dict[str, object]:
    """Read persisted V0.8 derived entities; raw search APIs stay unchanged."""

    places = await asyncio.to_thread(entity_repository.list_canonical, category=category, limit=limit)
    return {"count": len(places), "results": [place.model_dump(mode="json") for place in places]}


@app.get("/api/entities/debug/{canonical_id}")
async def debug_canonical_place(canonical_id: str) -> JSONResponse:
    """Development explainability view for one persisted canonical entity."""

    if not TOLL_DEBUG_MODE:
        return JSONResponse(status_code=404, content={"status": "not_found"})
    value = await asyncio.to_thread(entity_repository.debug, canonical_id)
    if value is None:
        return JSONResponse(status_code=404, content={"status": "not_found"})
    return JSONResponse(status_code=200, content=value)


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
        LOGGER.exception("Unhandled routing failure")
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
