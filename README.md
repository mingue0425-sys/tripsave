# Korea Trip Optimizer V0.3

Korea Trip Optimizer V0.3 uses MapLibre GL JS with the OpenFreeMap Liberty
vector basemap. The previous hand-authored GeoJSON preview is no longer a
runtime basemap. The application keeps its origin/destination selection layer
and route layer separate from the basemap so a future self-hosted OpenMapTiles
style can be swapped in without changing marker, selection, or route logic.

V0.3 adds a real car route through a local OSRM instance prepared from a South
Korea OpenStreetMap PBF. The browser calls FastAPI; it never calls OSRM or a
public routing API directly.

During development, the browser requests only the configured OpenFreeMap
basemap resources externally. There are no Google, Kakao, Naver, Mapbox,
external geocoder or routing-service, analytics, telemetry, external-font, or
CDN dependencies.

## Requirements

- Python 3.11 or newer
- A modern browser with WebGL support
- Internet access while using the OpenFreeMap development basemap
- A local OSRM runtime and prepared South Korea routing data for route tests

MapLibre GL JS 4.7.1 is vendored under `static/vendor/`; Node.js is not needed
to run the application.

## Installation

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run

```bash
python -m uvicorn app:app --reload
```

Open `http://127.0.0.1:8000` in a browser. The health endpoint is `/health`
and local city search is `/api/places/search?q=부산`.

## Basemap provider

The provider registry is in `config.py`:

```text
defaultBasemap: liberty
provider: OpenFreeMap
style: https://tiles.openfreemap.org/styles/liberty
```

The browser uses the provider style directly through MapLibre. The provider
style supplies OpenMapTiles vector data for roads, buildings, parks, water,
boundaries, places, and labels. Attribution remains visible on the map:

```text
OpenFreeMap © OpenMapTiles Data from OpenStreetMap
```

The development provider is documented by [OpenFreeMap's Quick Start
Guide](https://openfreemap.org/quick_start/). Its public style endpoint is
replaceable through the provider registry only.

## Location selection

Each card has two ways to set a location:

1. Select `지도에서 선택`, then click the map. The click is stored with
   `source: "map"`.
2. Select `검색`, enter a city name or English alias, then choose a result. The
   result is stored with `source: "local_search"`.

The A marker is the origin and the B marker is the destination. Existing
locations can be changed or deleted independently. `A ↔ B` swaps complete
location objects, and `전체 초기화` removes both locations and saved state.

Locations use this canonical object shape:

```json
{
  "lat": 35.1796,
  "lng": 129.0756,
  "label": "부산",
  "source": "local_search"
}
```

GeoJSON array order is not used by the application state; ordinary location
objects always use explicit `lat` and `lng` fields. Same/nearby locations
within 20 metres are rejected using a small Haversine selection guard.

## Local search

The local search is an internal FastAPI endpoint backed by the separate
`static/data/places.json` application dataset. It supports trimmed partial
matching, Unicode normalization, case-insensitive English aliases, and at
most eight results. It covers the current bundled city index, not a nationwide
address database or geocoder.

## Persistence and public browser interfaces

The selection is saved in `localStorage` under `koreaTrip.selection.v1` with
payload version `1`. Restore validates JSON, version, text fields, coordinate
ranges, finite numbers, and allowed sources. Malformed or unknown-version data
is removed safely.

The public browser interfaces are intentionally small:

- `window.KoreaTripMap`: basemap initialization, viewport, click coordinates,
  attribution, and V0.1-compatible marker fallback.
- `window.KoreaTripSelection`: `getOrigin`, `getDestination`, `setOrigin`,
  `setDestination`, `setSelectionMode`, `swap`, `clear`, and independent
  removal methods.
- `window.KoreaTripMarkerManager`: application marker lifecycle.
- `window.KoreaTripRoute`: route calculation, state, and invalidation.
- `window.KoreaTripRouteLayer`: separate GeoJSON route source/layer and fit
  bounds operations.

## Tests

```bash
python -m pytest -q -m "not integration"
node --test tests/test_storage.js tests/test_route_format.js
python scripts/verify_offline.py
```

The fast Python tests cover the server regression endpoints, local search,
canonical routing models, mocked OSRM failures, provider configuration,
retired preview removal, and fixed static-file safety. The integration suite
requires a running local OSRM and can be run with:

```bash
python -m pytest -q -m integration
```

The Node tests cover localStorage save/restore, malformed payload handling,
coordinate validation, the 20-metre guard, and route metric formatting.

For the actual Chromium basemap and selection smoke test, start a server on
port 8765 and use the existing local Playwright tooling:

```powershell
# Terminal 1: local OSRM
python scripts/setup_routing.py --start

# Terminal 2: FastAPI
python -m uvicorn app:app --host 127.0.0.1 --port 8765

# Terminal 3; .map-build is verification tooling, not an app dependency
$env:NODE_PATH = (Resolve-Path .map-build\node_modules)
node scripts\browser_smoke.js
```

The browser test records every request, verifies that only the local app and
configured basemap hosts are contacted, checks attribution and OpenMapTiles
source layers, visits twelve Korean regions, exercises selection and reload,
and calculates and invalidates routes through the local API.

## Network audit

`python scripts/verify_offline.py` is now an allowlist audit rather than a
zero-network check. `tiles.openfreemap.org` is the only approved external
basemap host. The browser audit must still report zero requests to commercial
map providers, public OSM tile servers, public routing services, geocoders,
analytics, or telemetry. The browser route request is relative FastAPI; the
FastAPI-to-OSRM request stays on loopback.

## Future self-hosted basemap

The intended production migration is:

```text
South Korea OSM PBF
        ↓
OpenMapTiles generation
        ↓
MBTiles/vector tiles
        ↓
TileServer GL
        ↓
MapLibre GL JS
```

Add a provider entry in `config.py` with the local style URL, provider name,
attribution, and request hostnames, then change `DEFAULT_BASEMAP_ID`. The
application marker, selection, and future route/POI layers do not need to be
rewritten. See [maps/README.md](maps/README.md) for the migration notes and
[OpenMapTiles' generation documentation](https://openmaptiles.org/docs/generate/generate-openmaptiles/).

## Local Routing Setup

The route graph is not committed to Git. Download the current South Korea OSM
extract from [Geofabrik](https://download.geofabrik.de/asia/south-korea.html)
to `maps/source/south-korea-latest.osm.pbf` with checksum verification:

```powershell
python scripts/setup_routing.py --download
```

Prepare and check the OSRM MLD graph, then run the local service:

```powershell
python scripts/setup_routing.py --preprocess
python scripts/setup_routing.py --check
python scripts/setup_routing.py --start
```

`--start` runs `osrm-routed --algorithm mld` on `127.0.0.1:5000`. It never
falls back to a public OSRM server. The current Windows-native setup was
verified with Python 3.14 and `osrm-bindings`; Python 3.11 users can use
Docker Desktop or WSL2. For Docker, preprocess and run with the same pinned
image family:

```powershell
python scripts/setup_routing.py --engine docker --preprocess
docker compose -f docker-compose.osrm.yml up
```

The compose image is `ghcr.io/project-osrm/osrm-backend:26.7.3-debian` and
publishes only `127.0.0.1:5000`. See [maps/README.md](maps/README.md) for the
PBF location, observed data sizes, generated files, and native/Docker notes.
The official workflow is documented in the [OSRM backend repository](https://github.com/project-osrm/osrm-backend)
and [OSRM tools documentation](https://project-osrm.org/docs/v26.4.0/tools).

The route endpoints are:

```text
GET  /api/routing/status
POST /api/routes
```

`POST /api/routes` accepts explicit `lat`/`lng` locations and returns the
canonical `distance_m`, `duration_s`, and GeoJSON `LineString` route schema.
OSRM's `[lng,lat]` coordinate convention is used only at the OSRM/GeoJSON
boundary; ordinary location objects always use `lat` and `lng`. Route results
are validated for finite coordinates, valid geometry, sane distance, and
endpoint snap distance before reaching the browser.

The backend applies a deliberately broad South Korea dataset box (latitude
33.0–38.7, longitude 124.5–132.0) to reject clearly out-of-dataset requests.
It is not a land polygon; sea/land matching is left to OSRM's road snap and
no-route behavior. Locations within 20 metres are rejected, and endpoint
snapping farther than 5 km is rejected instead of silently producing a
misleading route.

## Route behavior and limitations

With both locations selected, **경로 계산** requests one local OSRM car route.
The route is rendered in its own MapLibre source/layer and the viewport fits
the route. Changing either location, swapping A/B, or clearing the selection
removes the stale route; route geometry is not persisted in localStorage.

V0.3 does not implement tolls, fuel costs, accommodation, restaurants,
attractions, reviews, recommendations, itineraries, multi-stop routing,
traffic-aware routing, public transit, walking, or cycling. Local search is
still limited to the bundled city dataset.

## V0.3 boundaries

This version does not implement tolls, fuel costs, crawling, recommendations,
or a nationwide geocoder. The development basemap may request resources from
the explicitly configured OpenFreeMap host; route computation is local-only.
