"""Application paths and local routing/provider configuration."""

import os
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"
PLACES_DATA_FILE = STATIC_DIR / "data" / "places.json"

APP_VERSION = "0.4.0"
PLACES_SEARCH_URL = "/api/places/search"
ROUTE_API_URL = "/api/routes"
ROUTING_STATUS_URL = "/api/routing/status"
TOLL_STATUS_URL = "/api/tolls/status"
TOLL_API_URL = "/api/tolls/calculate"

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
OFFICIAL_TOLL_URL = "https://www.ex.co.kr/portal/usefee/selectUseFeeNList.do"
OFFICIAL_TOLL_HOSTNAME = "www.ex.co.kr"
TOLL_CACHE_TTL_DAYS = 30
TOLL_REQUEST_TIMEOUT_S = 20.0
TOLL_CONNECT_TIMEOUT_S = 5.0
TOLL_REQUEST_INTERVAL_S = 1.5
# OSM gate points and the OSRM geometry are normally coincident; 75 m leaves
# room for extract/geometry differences without treating nearby ramps and
# parallel carriageways several hundred metres away as the travelled gate.
TOLL_GATE_MATCH_THRESHOLD_M = 75.0
TOLL_GATE_DEDUP_THRESHOLD_M = 150.0
TOLL_ROAD_MATCH_THRESHOLD_M = 100.0
TOLL_MAX_PRICE_KRW = 10_000_000
TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_M = 50_000.0
TOLL_MAX_OFFICIAL_DISTANCE_MISMATCH_RATIO = 0.35


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
        "routeApiUrl": ROUTE_API_URL,
        "routingStatusUrl": ROUTING_STATUS_URL,
        "tollStatusUrl": TOLL_STATUS_URL,
        "tollApiUrl": TOLL_API_URL,
        "selectionStorageKey": "koreaTrip.selection.v1",
    }
