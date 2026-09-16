"""Application paths and local routing/provider configuration."""

import os
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"
PLACES_DATA_FILE = STATIC_DIR / "data" / "places.json"

APP_VERSION = "1.0.0"
PLACES_SEARCH_URL = "/api/places/search"
ACCOMMODATION_API_URL = "/api/accommodations/search"
ENTITY_RESOLVE_API_URL = "/api/entities/resolve"
CANONICAL_PLACES_API_URL = "/api/places/canonical"
TRIP_CANDIDATES_API_URL = "/api/trips/candidates"
RECOMMENDATIONS_API_URL = "/api/recommendations/rank"
DEBUG_RECOMMENDATIONS_API_URL = "/api/debug/recommendations/rank"
ROUTE_API_URL = "/api/routes"
ROUTING_STATUS_URL = "/api/routing/status"
TOLL_STATUS_URL = "/api/tolls/status"
TOLL_API_URL = "/api/tolls/calculate"
FUEL_API_URL = "/api/fuel/calculate"
FUEL_STATUS_URL = "/api/fuel/status"
DRIVING_COST_API_URL = "/api/costs/driving"

LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
OSRM_BASE_URL = os.getenv("KTO_OSRM_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
OSRM_BIND_HOST = "127.0.0.1"
OSRM_PORT = 5000
OSRM_PROFILE = "car"
OSRM_REQUEST_TIMEOUT_S = 10.0
OSRM_CONNECT_TIMEOUT_S = 3.0
OSRM_MAX_SNAP_DISTANCE_M = 5_000.0
SAME_LOCATION_THRESHOLD_METERS = 20.0
# Deliberately broad dataset guard: it includes Jeju, Ulleungdo, and Dokdo,
# while preventing requests for clearly out-of-dataset countries. Sea/land
# membership remains an OSRM snap/no-route concern rather than a box test.
SOUTH_KOREA_ROUTING_BOUNDS = (33.0, 38.7, 124.5, 132.0)

ROUTING_PBF_FILE = PROJECT_ROOT / "maps" / "source" / "south-korea-latest.osm.pbf"
ROUTING_DATA_BASE = PROJECT_ROOT / "maps" / "osrm" / "south-korea-latest"
OSM_PBF_URL = "https://download.geofabrik.de/asia/south-korea-latest.osm.pbf"
OSM_PBF_CHECKSUM_URL = f"{OSM_PBF_URL}.md5"
OSRM_DOCKER_IMAGE = "ghcr.io/project-osrm/osrm-backend:26.7.3-debian"

# Toll infrastructure is local OSM-derived data plus a server-side adapter for
# the official Korea Expressway HTML page.  The page URL is a fixed source;
# clients never supply a crawler URL.
TOLL_INDEX_DB = PROJECT_ROOT / "data" / "korea_trip.db"
# Accommodation offer/metadata rows intentionally live in their own SQLite
# file.  The toll index and fuel cache share a read/write database with a
# different schema and lifecycle; accommodation searches must never contend
# with that hot path.
ACCOMMODATION_CACHE_DB = PROJECT_ROOT / "data" / "accommodation_cache.sqlite3"
# V0.8 canonical entities are derived data and intentionally use a third DB;
# neither V0.6 accommodation rows nor V0.7 place rows are migrated or deleted.
ENTITY_RESOLUTION_DB = PROJECT_ROOT / "data" / "entity_resolution.sqlite3"
# Candidate sets are ephemeral assembled inputs for recommendation ranking.
# Keeping them in their own SQLite database makes the candidate-ID API
# consistent across threads and Uvicorn workers without mixing raw source
# caches with application state.
CANDIDATE_SET_DB = PROJECT_ROOT / "data" / "trip_candidates.sqlite3"
CANDIDATE_SET_TTL_S = float(os.getenv("KTO_CANDIDATE_SET_TTL_S", "3600"))
CANDIDATE_SET_MAX_SETS = int(os.getenv("KTO_CANDIDATE_SET_MAX_SETS", "1000"))
OFFICIAL_TOLL_URL = "https://www.ex.co.kr/portal/usefee/selectUseFeeNList.do"
OFFICIAL_TOLL_HOSTNAME = "www.ex.co.kr"
TOLL_CACHE_TTL_DAYS = 30
# A verified result may be served immediately after its fresh TTL while a
# low-priority refresh runs.  Values older than this window are no longer
# trusted as a usable fallback and therefore behave like a cache miss.
TOLL_STALE_MAX_AGE_DAYS = int(os.getenv("KTO_TOLL_STALE_MAX_AGE_DAYS", "180"))
# A failed official pair is never treated as a price.  This short in-memory
# cooldown only prevents repeatedly paying the HTTP/browser failure latency
# for a known unavailable direction; it expires and is retried automatically.
TOLL_FAILURE_COOLDOWN_S = float(os.getenv("KTO_TOLL_FAILURE_COOLDOWN_S", "30"))
TOLL_REQUEST_TIMEOUT_S = 20.0
TOLL_CONNECT_TIMEOUT_S = 5.0
TOLL_REQUEST_INTERVAL_S = 1.5
TOLL_HTTP_MAX_CONNECTIONS = int(os.getenv("KTO_TOLL_HTTP_MAX_CONNECTIONS", "2"))
TOLL_HTTP_MAX_KEEPALIVE_CONNECTIONS = int(
    os.getenv("KTO_TOLL_HTTP_MAX_KEEPALIVE_CONNECTIONS", "2")
)
# OSM gate points and the OSRM geometry are normally coincident; 75 m leaves
# room for extract/geometry differences without treating nearby ramps and
# parallel carriageways several hundred metres away as the travelled gate.
TOLL_GATE_MATCH_THRESHOLD_M = 75.0
TOLL_GATE_DEDUP_THRESHOLD_M = 150.0
TOLL_ROAD_MATCH_THRESHOLD_M = 100.0
TOLL_MAX_PRICE_KRW = 10_000_000
TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_M = 50_000.0
TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_RATIO = 0.35

# Opinet's public HTML statistics are the only V0.5 fuel-price source.  The
# two pages are separate because gasoline/diesel and automotive LPG are
# published by different public result views.
FUEL_LIQUID_PRICE_URL = "https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do"
FUEL_LPG_PRICE_URL = "https://www.opinet.co.kr/user/dopvsavsel/dopVsAvselSelect.do"
OPINET_HOSTNAME = "www.opinet.co.kr"
FUEL_CACHE_TTL_S = float(os.getenv("KTO_FUEL_CACHE_TTL_S", "10800"))
FUEL_REQUEST_INTERVAL_S = float(os.getenv("KTO_FUEL_REQUEST_INTERVAL_S", "2.0"))
FUEL_BROWSER_NAVIGATION_TIMEOUT_S = float(
    os.getenv("KTO_FUEL_BROWSER_NAVIGATION_TIMEOUT_S", "30")
)
FUEL_BROWSER_SELECTOR_TIMEOUT_S = float(
    os.getenv("KTO_FUEL_BROWSER_SELECTOR_TIMEOUT_S", "15")
)
FUEL_BROWSER_RESULT_TIMEOUT_S = float(
    os.getenv("KTO_FUEL_BROWSER_RESULT_TIMEOUT_S", "30")
)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


FUEL_BROWSER_HEADLESS = _env_bool("KTO_FUEL_BROWSER_HEADLESS", True)
FUEL_BROWSER_ARTIFACT_DIR = (
    Path(
        os.getenv(
            "KTO_FUEL_DEBUG_ARTIFACT_DIR",
            str(PROJECT_ROOT / "artifacts" / "fuel-debug"),
        )
    )
    if _env_bool("KTO_FUEL_DEBUG_ARTIFACTS", False)
    else None
)


# HTTP is the production primary path. Playwright is kept warm as an
# exceptional fallback. These separate timeouts distinguish navigation, DOM
# readiness, and result submission so a slow official page is diagnosable
# without allowing an infinite wait.
TOLL_BROWSER_NAVIGATION_TIMEOUT_S = float(os.getenv("KTO_TOLL_BROWSER_NAVIGATION_TIMEOUT_S", "30"))
TOLL_BROWSER_SELECTOR_TIMEOUT_S = float(os.getenv("KTO_TOLL_BROWSER_SELECTOR_TIMEOUT_S", "15"))
TOLL_BROWSER_RESULT_TIMEOUT_S = float(os.getenv("KTO_TOLL_BROWSER_RESULT_TIMEOUT_S", "30"))
TOLL_BROWSER_HEADLESS = _env_bool("KTO_TOLL_BROWSER_HEADLESS", True)
# Launch Chromium during application startup so an exceptional browser
# fallback does not put process startup on the user request's critical path.
# Set KTO_TOLL_BROWSER_WARMUP=0 for deployments that prefer lazy resources.
TOLL_BROWSER_WARMUP = _env_bool("KTO_TOLL_BROWSER_WARMUP", True)
TOLL_DEBUG_MODE = _env_bool("KTO_DEBUG", False)
TOLL_BROWSER_ARTIFACT_DIR = (
    Path(os.getenv("KTO_TOLL_DEBUG_ARTIFACT_DIR", str(PROJECT_ROOT / "artifacts" / "toll-debug")))
    if _env_bool("KTO_TOLL_DEBUG_ARTIFACTS", False)
    else None
)


def validate_official_toll_url(value: str) -> str:
    """Allow only the configured official HTML toll inquiry page."""

    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OFFICIAL_TOLL_HOSTNAME
        or parsed.path != "/portal/usefee/selectUseFeeNList.do"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "OFFICIAL_TOLL_URL must be the fixed Korea Expressway toll HTML page."
        )
    return value


def validate_local_osrm_base_url(value: str) -> str:
    """Reject non-local OSRM endpoints to prevent routing SSRF/fallbacks."""

    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTNAMES:
        raise ValueError(
            "OSRM_BASE_URL must be an HTTP URL hosted on localhost, 127.0.0.1, or ::1."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("OSRM_BASE_URL must not contain credentials, query, or fragment data.")
    if parsed.path not in {"", "/"}:
        raise ValueError("OSRM_BASE_URL must point to the OSRM server root.")
    return value.rstrip("/")


OSRM_BASE_URL = validate_local_osrm_base_url(OSRM_BASE_URL)

# Basemap provider configuration is the only place application code needs to
# know which style host is used. Swap this style URL for a self-hosted
# OpenMapTiles/TileServer GL style without changing map or marker logic.
BASEMAPS = {
    "liberty": {
        "id": "liberty",
        "provider": "OpenFreeMap",
        "styleUrl": "https://tiles.openfreemap.org/styles/liberty",
        "attribution": "OpenFreeMap © OpenMapTiles Data from OpenStreetMap",
        "requestHostnames": ["tiles.openfreemap.org"],
    }
}
DEFAULT_BASEMAP_ID = "liberty"


def map_config() -> dict[str, object]:
    """Return a serializable browser-facing map/provider configuration."""

    basemaps = {
        basemap_id: dict(definition)
        for basemap_id, definition in BASEMAPS.items()
    }
    return {
        "defaultBasemap": DEFAULT_BASEMAP_ID,
        "basemaps": basemaps,
        "placesSearchUrl": PLACES_SEARCH_URL,
        "accommodationApiUrl": ACCOMMODATION_API_URL,
        "entityResolveApiUrl": ENTITY_RESOLVE_API_URL,
        "canonicalPlacesApiUrl": CANONICAL_PLACES_API_URL,
        "tripCandidatesApiUrl": TRIP_CANDIDATES_API_URL,
        "recommendationsApiUrl": RECOMMENDATIONS_API_URL,
        "routeApiUrl": ROUTE_API_URL,
        "routingStatusUrl": ROUTING_STATUS_URL,
        "tollStatusUrl": TOLL_STATUS_URL,
        "tollApiUrl": TOLL_API_URL,
        "fuelApiUrl": FUEL_API_URL,
        "fuelStatusUrl": FUEL_STATUS_URL,
        "drivingCostApiUrl": DRIVING_COST_API_URL,
        "selectionStorageKey": "koreaTrip.selection.v1",
        "debug": TOLL_DEBUG_MODE,
    }
