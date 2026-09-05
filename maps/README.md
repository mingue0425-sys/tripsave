# Basemap provider and local routing data

The development basemap is the OpenFreeMap Liberty style configured in
`config.py`. It is a real OpenMapTiles/OpenStreetMap vector basemap, so the
browser fetches style, glyph, sprite, and vector-tile resources from the
configured provider while the app is running.

The application does not serve or render the retired
`maps/tiles/korea-basemap.geojson` preview. City search data is separate
application data under `static/data/places.json` and is not a basemap.

## V0.3 South Korea routing dataset

Routing uses a separate South Korea OpenStreetMap extract. It is not the
browser basemap and is never downloaded or queried by the application at
runtime. Get the current extract from
[Geofabrik](https://download.geofabrik.de/asia/south-korea.html) with:

```powershell
python scripts/setup_routing.py --download
```

The expected source path is:

```text
maps/source/south-korea-latest.osm.pbf
```

The extract observed while building V0.3 was 286,625,768 bytes (about 273.3
MiB) with MD5 `9a4e7b5c32df7d038440099b22ce6268`. Geofabrik extracts change;
these values are an observation, not a permanent data pin. The PBF is ignored
by Git.

## OSRM MLD graph

The project uses the OSRM `car` profile and the MLD pipeline:

```text
osrm-extract -p car.lua -o maps/osrm/south-korea-latest.osrm \
  maps/source/south-korea-latest.osm.pbf
osrm-partition maps/osrm/south-korea-latest.osrm
osrm-customize maps/osrm/south-korea-latest.osrm
```

Use the setup helper to select the available native or Docker toolchain:

```powershell
python scripts/setup_routing.py --preprocess
python scripts/setup_routing.py --check
```

The generated base path is:

```text
maps/osrm/south-korea-latest.osrm
```

The generated directory contains the `.osrm.*` MLD graph artifacts, including
partition/cell metrics, geometry, edge/node data, and properties. In this
workspace the generated graph artifacts occupied about 1.83 GiB across 26
files; actual size depends on the extract and OSRM build. All generated files
are ignored by Git (`maps/osrm/*`, except `.gitkeep`).

## Native and Docker runtimes

The Windows-native setup was verified with Python 3.14 and the optional
`osrm-bindings` wheel installed from `requirements.txt`. It supplies the
OSRM executables and official `car.lua` profile. The setup helper invokes the
executables directly.

Docker Desktop is the cross-platform alternative:

```powershell
python scripts/setup_routing.py --engine docker --preprocess
docker compose -f docker-compose.osrm.yml up
```

The pinned image is
`ghcr.io/project-osrm/osrm-backend:26.7.3-debian`; it publishes only
`127.0.0.1:5000`. Preprocess and run with the same OSRM version family. Do
not mix native-generated graph data with a different Docker runtime without
an explicit compatibility check.

The local server is equivalent to:

```powershell
osrm-routed --algorithm mld --ip 127.0.0.1 --port 5000 `
  maps/osrm/south-korea-latest.osrm
```

`python scripts/setup_routing.py --start` runs this in the foreground and
waits for its local health endpoint. Missing data or tools produce setup
instructions; there is no public OSRM fallback.

## Attribution

South Korea OSM data is © OpenStreetMap contributors and is licensed under
the [Open Database License](https://www.openstreetmap.org/copyright). Keep
the required attribution when redistributing derived routing data. Basemap
attribution is separate and remains visible through the OpenFreeMap/MapLibre
control.

## Development provider

The current provider entry is:

```text
id: liberty
provider: OpenFreeMap
style: https://tiles.openfreemap.org/styles/liberty
```

The UI keeps the provider attribution visible:

```text
OpenFreeMap © OpenMapTiles Data from OpenStreetMap
```

Only the configured basemap host is allowed in the browser network audit.
Google, Kakao, Naver, Mapbox, public OSM tile servers, geocoders, routing
services, analytics, and telemetry remain forbidden.

## Future self-hosted migration

The intended production flow is:

```text
South Korea OSM PBF
        ↓
OpenMapTiles generation
        ↓
MBTiles or vector tiles
        ↓
TileServer GL (local)
        ↓
MapLibre GL JS
```

To migrate, add a new provider entry in `config.py` with the self-hosted style
URL, provider name, attribution, and request hostnames, then change
`DEFAULT_BASEMAP_ID`. `map.js`, marker management, selection state, and future
application layers do not need to know the tile server implementation.

The PBF and generated tile artifacts are intentionally ignored by Git:

```text
maps/source/south-korea-latest.osm.pbf
maps/tiles/*.pmtiles
maps/tiles/*.mbtiles
```

Do not download large extracts or invoke a remote conversion service from the
application. Tile generation remains an explicit, offline operator workflow.
