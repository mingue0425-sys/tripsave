# Korea Trip Optimizer V0.5

Korea Trip Optimizer V0.3 uses MapLibre GL JS with the OpenFreeMap Liberty
vector basemap. The previous hand-authored GeoJSON preview is no longer a
runtime basemap. The application keeps its origin/destination selection layer
and route layer separate from the basemap so a future self-hosted OpenMapTiles
style can be swapped in without changing marker, selection, or route logic.

V0.3 adds a real car route through a local OSRM instance prepared from a South
Korea OpenStreetMap PBF. V0.4 adds a local OSM toll-gate index and a
server-side adapter for the official Korea Expressway toll inquiry page. V0.5
adds route-bound fuel cost calculation for gasoline, diesel, and LPG using the
user's actual efficiency and the current national-average price shown by
Opinet's public HTML pages. The browser calls FastAPI; it never calls OSRM, a
toll API, a fuel API, or a public routing API directly.

During development, the browser requests only the configured OpenFreeMap
basemap resources externally. There are no Google, Kakao, Naver, Mapbox,
external geocoder or routing-service, analytics, telemetry, external-font, or
CDN dependencies.

## Requirements

- Python 3.11 or newer
- A modern browser with WebGL support
- Internet access while using the OpenFreeMap development basemap, the
  official HTML toll inquiry source, and the official Opinet HTML price source
- A local OSRM runtime and prepared South Korea routing data for route tests

MapLibre GL JS 4.7.1 is vendored under `static/vendor/`; Node.js is not needed
to run the application.

## Installation

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
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

Open `http://127.0.0.1:8000` in a browser. The health endpoint is `/health`,
local city search is `/api/places/search?q=부산`, and local OSRM status is
`/api/routing/status`.

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
retired preview removal, fixed static-file safety, toll models, matching,
toll parser/cache/API behavior, fuel models, fuel arithmetic, Opinet parser,
fuel cache, source fallback, driving-cost aggregation, and API behavior. The integration suite
requires a running local OSRM and can be run without the live official source
with:

```bash
python -m pytest -q -m "integration and not official"
```

The marked V0.5 representative integration covers 서울→부산, 서울→대전,
부산→대구, 강릉→서울, and 대전→광주 with the live route, toll, and public
fuel-price services:

```bash
python -m pytest -q tests/fuel/test_cost_integration.py -m "integration and official"
```

The live official HTML adapter tests are separately marked and require network
access to the fixed [한국도로공사 통행요금조회 페이지](https://www.ex.co.kr/portal/usefee/selectUseFeeNList.do)
and the [Opinet 제품별 평균공급가격 페이지](https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do):

```bash
python -m playwright install chromium
python -m pytest -q -m official
```

The official adapter is HTTP-first. It reproduces the observed public flow:
initial HTML, `pathCheckN.do` station validation, the canonical HTML form
submission, and route-table parsing. It validates status, content type,
redirect host, entry/exit identity, all vehicle prices, and official distance.
The returned `nosunCd` station IDs are stored with the full directional pair
price table; raw OSM gate names are only a first-read alias. A validated HTTP
failure can use the pooled Playwright browser fallback, while an explicit
access/robots denial is not bypassed. Set
`KTO_TOLL_BROWSER_EXECUTABLE_PATH` only when a deployment needs to point
Playwright at an already installed Chromium binary.

The Node tests cover localStorage save/restore, malformed payload handling,
coordinate validation, the 20-metre guard, and route metric formatting. Run
the V0.4 JavaScript syntax checks with:

```powershell
node --check static/js/toll.js
node --check static/js/cost.js
node --check static/js/toll_markers.js
node --check static/js/route.js
```

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

The V0.4 route/toll lifecycle smoke test uses the same setup and is run with:

```powershell
node scripts\toll_browser_smoke.js
```

The V0.5 driving-cost Chromium E2E uses the installed Python Playwright
package and verifies an actual Seoul→Daejeon route, official gasoline and
diesel prices, fuel-efficiency-only recalculation, fuel-type invalidation,
route invalidation, toll-panel synchronization, network allowlisting, and
console errors:

```bash
KTO_CHROME_PATH="/path/to/Google Chrome for Testing" \
  python scripts/cost_browser_smoke.py
```

On this workspace the same executable can be supplied to the server-side
fuel/toll browser clients with `KTO_FUEL_BROWSER_EXECUTABLE_PATH` and
`KTO_TOLL_BROWSER_EXECUTABLE_PATH` when the default Playwright browser path is
not installed.

The browser test records every request, verifies that only the local app and
configured basemap hosts are contacted, checks attribution and OpenMapTiles
source layers, visits twelve Korean regions, exercises selection and reload,
and calculates and invalidates routes through the local API. The V0.4 smoke
test additionally checks the toll endpoint's canonical result/partial-result
handling, TG marker rendering, vehicle-class invalidation, and route/toll
stale-state protection.

## Network audit

`python scripts/verify_offline.py` is now an allowlist audit rather than a
zero-network check. The browser-side map host remains `tiles.openfreemap.org`;
the server-side V0.4/V0.5 source adapters additionally allow only the fixed
official Korea Expressway and Opinet HTML hosts. The browser audit must still
report zero requests to commercial map providers, public OSM tile servers,
public routing services, geocoders, analytics, or telemetry. The browser route
and cost requests are relative FastAPI; the FastAPI-to-OSRM request stays on
loopback.

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

## Local Toll Calculation

Build the toll index after the South Korea PBF and its local OSRM graph are
available:

```powershell
python scripts/build_tollgate_index.py
```

Use `--force` only when intentionally rebuilding the generated SQLite file.
The builder reads the PBF and records OSM `barrier=toll_booth`,
`highway=toll_gantry`, and `toll=yes` road evidence in
`data/korea_trip.db`. Generated data is ignored by Git. The observed PBF
produced 2,195 gate features, 20,398 raw toll-tagged ways, and 20,237
vehicle-road ways retained by the index in this workspace; these are data
observations, not permanent pins.

The toll endpoints are:

```text
GET  /api/tolls/status
POST /api/tolls/calculate
GET  /api/tolls/debug/{route_id}  (only when KTO_DEBUG=1)
```

The backend matches the canonical OSRM route against local OSM evidence and,
when the journey is supported, uses the verified HTTP flow to query the
normal public HTML form at
[한국도로공사 통행요금조회 페이지](https://www.ex.co.kr/portal/usefee/selectUseFeeNList.do).
It uses the official entry/exit result as the journey total; it never sums
individual gate prices. The source adapter uses a reusable HTTP connection
pool, pair-level full vehicle-price caching with a 30-day fresh TTL and a
180-day bounded stale-usable window, keyed single-flight, and a pooled
Playwright fallback. Stale values are returned immediately with their last
official timestamp while refresh runs in the background. Station mapping and
HTTP/browser request evidence are retained in development diagnostics;
optional screenshots and a result HTML fragment are written only when
`KTO_TOLL_DEBUG_ARTIFACTS=1`. It does not use an OpenAPI endpoint, a private
API, or an alternative public service.

The supported authoritative operator in V0.4 is 한국도로공사. A mixed corridor
is not rejected merely because an adjacent or connected OSM way has another
operator: the official directional result and its distance must validate the
whole journey. A private/unknown-only corridor, an ambiguous gate match, or a
parser/source failure returns `complete: false` and never turns the unknown
amount into `0원`. The current sparse index does not treat an empty candidate
set as proof of a free route; it returns an incomplete result until free-road
coverage is explicitly verified. Vehicle classes are `class_1`, `compact`,
`class_2`, `class_3`, `class_4`, and `class_5`.

The UI displays detected OSM gate candidates as `TG` markers and keeps route,
toll, and selection lifecycles separate. Changing the route or vehicle class
invalidates the toll result. Toll results are not stored in localStorage;
origin/destination locations are stored and the toll is recalculated after a
reload.

Development diagnostics distinguish route success, raw candidate discovery,
logical gate deduplication, route-progress entry/exit candidates, official
directional station validation, page request, parsing, vehicle-price
extraction, and completion. The first/last named OSM features are only
candidates; exit-only or branch facilities are rejected by the official
station check before the valid pair is selected. The debug panel reports raw
candidate count separately from logical tollgate count and includes each
candidate's OSM identity, tags, route position, duplicate group, and role. An
incomplete result never turns an unknown amount into `0원`.

## Route behavior and limitations

With both locations selected, **경로 계산** requests one local OSRM car route.
The route is rendered in its own MapLibre source/layer and the viewport fits
the route. Changing either location, swapping A/B, or clearing the selection
removes the stale route; route geometry is not persisted in localStorage.

## Fuel Cost Calculation

V0.5 calculates fuel cost from the canonical local-OSRM route distance, the
user's entered **actual efficiency**, and a verified public-web price. The
supported internal fuel enum and Korean UI labels are:

```text
gasoline → 휘발유
diesel   → 경유
lpg      → LPG
```

Electric vehicles are intentionally outside V0.5. The efficiency field is
required for a fuel calculation and is validated on both sides of the API:
finite numeric values from `0.1` through `100` km/L are accepted. There is no
hidden default efficiency and no vehicle-efficiency database.

### Fuel Price Source

The source of truth is the public HTML rendered by the official [Opinet 제품별
평균공급가격 페이지](https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do)
for 보통휘발유 and 자동차용경유, and the official [Opinet 자동차충전소 평균
판매가격 페이지](https://www.opinet.co.kr/user/dopvsavsel/dopVsAvselSelect.do)
for 자동차부탄. The adapter first uses the verified server-rendered HTML GET
and falls back to a Playwright client that reads the same visible result DOM.
It does not use Opinet OpenAPI, a private endpoint, an API key, or a guessed
request parameter.

The parser matches semantic product labels (`보통휘발유`, `자동차용경유`,
`자동차부탄`), selects the latest dated row, verifies the displayed `원/리터`
or `원/ℓ` unit, and normalizes all three supported fuels to the internal
`krw_per_l` unit. Missing fields, changed HTML, denied access, timeout, and
unsupported units are explicit failures.

### Fuel Price Cache

Prices are stored in the existing SQLite database in `fuel_prices_cache`, keyed
by fuel type, scope, region, and normalized unit. The default TTL is three
hours (`KTO_FUEL_CACHE_TTL_S`); a cache hit avoids another public-web request.
If the source is temporarily unavailable, an expired verified row may be used
with `source_status: "stale"` and the UI says **최근 확인 가격 사용**. No
unavailable price is written or returned as zero. The cache also records the
observed date, fetch time, source URL, parser version, and a hash of the parsed
table evidence.

### Fuel Cost Formula

The arithmetic service is separate from the crawler:

```text
distance_km = route.distance_m / 1000
fuel_volume_l = distance_km / actual_efficiency_km_per_l
fuel_cost_krw = fuel_volume_l × price_krw_per_l
```

Floating-point precision is retained through the volume and raw-cost steps;
only the final KRW values are rounded half-up. `/api/fuel/calculate` accepts
the complete route payload (and optional route ID), so the server validates the
route geometry and does not trust an arbitrary client distance.

### Driving Cost and Round Trip (V0.5.1)

The aggregate endpoint is:

```text
GET  /api/fuel/status
POST /api/fuel/calculate
POST /api/costs/driving
```

`/api/costs/driving` combines official toll and fuel results without changing
the V0.4 fail-safe. The canonical aggregate is
`driving_cost.outbound`, `driving_cost.return`, and
`driving_cost.round_trip`; the JSON response does not use `one_way` as its
canonical leg name. Each leg exposes `distance_m`, `fuel_volume_l`,
`fuel_cost_krw`, `toll_krw`, and `total_krw`.

The arithmetic invariants are strict:

```text
round_trip.distance_m = outbound.distance_m + return.distance_m
round_trip.fuel_volume_l = outbound.fuel_volume_l + return.fuel_volume_l
round_trip.fuel_cost_krw = outbound.fuel_cost_krw + return.fuel_cost_krw
round_trip.toll_krw = outbound.toll_krw + return.toll_krw
round_trip.total_krw = round_trip.fuel_cost_krw + round_trip.toll_krw
```

When `round_trip_mode` is `directional`, the aggregate requests a reverse
local OSRM route and a reverse official toll. Return fuel therefore uses the
actual B→A route distance. If reverse routing fails, the return and round-trip
cost remain incomplete with `reason: "RETURN_ROUTE_UNAVAILABLE"`; no doubled
fuel fallback is presented as a normal complete round trip.

`round_trip_toll` separates amount availability from verification:

- `verified_official`: outbound and return official tolls are both available.
- `estimated_doubled_outbound`: only outbound official toll is available and
  the displayed return amount is an explicit estimate; `verified` and
  `complete` remain false.
- `unknown`: no safe toll amount is available; the API never turns it into
  zero.

Thus a numeric estimate may have `cost_complete: true` while
`officially_verified: false` and `contains_estimate: true`. A reverse-route
failure has `cost_complete: false`. The UI renders outbound, return, and
round-trip breakdowns independently and labels estimated tolls as estimates.

The browser invalidates the aggregate cost when the route, origin,
destination, fuel type, or vehicle class changes. Changing only the efficiency
recalculates fuel arithmetic from the already verified price without another
web crawl. Request IDs and `AbortController` prevent an older fuel selection
from overwriting a newer result.

### V0.5 boundaries and known limitations

- Fuel price is a national average, not a station-specific purchase quote.
- The user-provided actual efficiency does not model congestion, terrain,
  weather, air conditioning, or traffic-aware routing.
- LPG is normalized to the official page's displayed won-per-litre unit;
  energy-equivalent or won-per-kg comparisons are not attempted.
- Electric vehicles, charging prices, parking, accommodation, restaurants,
  attractions, reviews, recommendations, itineraries, multi-stop routing,
  public transit, walking, cycling, and a nationwide geocoder are out of scope.
- Official source HTML or access policy can change. The parser has bounded
  navigation/selector/result timeouts, low concurrency, limited fallback, and
  explicit failure states rather than guessed prices.

The V0.5 browser/network allowlist includes the local app, loopback OSRM,
OpenFreeMap's configured basemap host, the fixed Korea Expressway toll page,
and the two fixed Opinet public HTML pages. No Google, Kakao, Naver, Mapbox,
public OSRM, commercial fuel API, Opinet OpenAPI, analytics, telemetry, proxy
rotation, or CAPTCHA/access-control bypass is used.

## V0.8 Entity Resolution

V0.8 keeps V0.6/V0.7 source records intact and adds a derived canonical layer.
The legacy destination index is implemented by `backend/city_search.py`; the
`backend.places` package only re-exports that API for compatibility with older
callers.

The development endpoint is:

```text
POST /api/entities/resolve
GET  /api/places/canonical
GET  /api/entities/debug/{canonical_id}  # debug mode only
```

`POST /api/entities/resolve` accepts validated `PlaceSourceRecord`-shaped
records (including V0.7 raw fields) and separate `AccommodationOffer` rows.
Offers are linked to a canonical accommodation only by source and native place
identity; offers are never merged into `Place.price`.

The resolver uses category/region/name/spatial blocking, hard category/region/
distance/branch constraints, and the three decisions `MATCH`, `AMBIGUOUS`, and
`NO_MATCH`. A fuzzy name by itself is never a merge. Clusters are checked
pairwise, so a transitive A→B→C chain cannot force an inconsistent A/C merge.

Ratings are normalized to `0..1`; null ratings and review counts remain null.
The Bayesian prior is the current input batch's category median and `m` is its
75th-percentile observed review count. Cross-source review counts are exposed
as `observed_review_count_sum`, explicitly not as a unique-review count.

Canonical rows, memberships, candidate decisions, overrides, and linked offers
are stored in `data/entity_resolution.sqlite3`. This is a separate derived
database; the accommodation, places, toll, and fuel raw/cache databases are not
migrated or deleted. The matcher version and all source memberships make a
resolution explainable and rebuildable.

## V0.9 Trip Candidates and Cost Completeness

V0.9 assembles `TripCandidate` records from the existing route, driving-cost,
canonical-place, and accommodation-offer contracts. The endpoint is:

```text
POST /api/trips/candidates
```

The request may include prefetched dependencies for deterministic tests and
offline operation. Without them, the endpoint reuses the existing route,
driving-cost, accommodation, places, cache, and entity-resolution services;
source failures stay attached to their own component.

Accommodation prices are calculated only when the offer declares
`TOTAL_STAY` or `PER_NIGHT`, with `final_price_krw` taking precedence over a
fully known base-plus-tax breakdown. An unknown basis, missing tax, expired
offer, unavailable source, or missing route never becomes zero. Every required
component is marked `VERIFIED`, `ESTIMATED`, or `UNKNOWN`; a complete verified
total is exposed as `total_krw`, while a partial candidate exposes only its
`known_subtotal_krw` and `missing_components`.

The current required overnight total is driving plus accommodation. A day trip
has no required accommodation component. Restaurant and attraction counts,
rating means, coordinate coverage, and rating confidence are descriptive
`TripQualityFeatures` only; V0.9 does not rank candidates or create a final
recommendation score. Candidate IDs and ordering are deterministic and each
accommodation offer remains a separate candidate input.

The browser’s `여행 후보` panel uses DOM APIs and displays verified totals,
estimated totals, and incomplete totals with separate wording. Nullable rating,
price, and review fields remain unknown in the UI as well.

## V1.0 Explainable Recommendations

V1.0 ranks already assembled `TripCandidate` records through:

```text
POST /api/recommendations/rank
```

The ranker supports `balanced`, `lowest_cost`, `value`,
`accommodation_quality`, `sightseeing`, and `low_driving` modes, plus validated
custom weights for cost, accommodation, restaurants, attractions, and driving.
Weights are normalized only after unavailable features are removed. A missing
feature remains `null`; it is never silently converted to a zero-quality score.

Total-cost ranking uses `total_krw` only. Partial `known_subtotal_krw` candidates
cannot appear as cheap complete trips, while estimated-complete candidates are
allowed with a lower confidence value and an explicit warning. Rating features
use the V0.8 normalized rating and confidence, place counts use diminishing
returns, and driving score uses distance/duration rather than adding driving
cost a second time.

Every recommendation includes normalized feature scores, confidence,
strengths, weaknesses, warnings, and weighted contribution traces. Ranking is
deterministic, uses stable tie-breaks, and performs no crawler, OSRM, or toll
request. The browser recommendation panel supports preset modes and accessible
custom-weight sliders; changing any candidate dependency invalidates the old
ranking.
