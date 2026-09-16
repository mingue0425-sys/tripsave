#!/usr/bin/env python3
"""Build the local POI index from an existing OSM PBF.

This script intentionally never downloads a map or calls a third-party API.
Run it only after the configured local PBF has been obtained through the
project's normal map-data process.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.poi.models import PoiCategory, PoiRecord
from backend.poi.repository import PoiRepository
from config import POI_INDEX_DB, ROUTING_PBF_FILE


def _categories(tags: dict[str, str]) -> list[PoiCategory]:
    result: list[PoiCategory] = []
    amenity = tags.get("amenity", "").casefold()
    emergency = tags.get("emergency", "").casefold()
    if amenity in {"hospital", "clinic"}:
        result.append(PoiCategory.HOSPITAL)
    if amenity in {"hospital", "clinic"} and emergency in {"yes", "emergency_centre", "emergency_center"}:
        result.append(PoiCategory.EMERGENCY)
    if emergency in {"emergency_centre", "emergency_center"}:
        result.append(PoiCategory.EMERGENCY)
    if amenity == "pharmacy":
        result.append(PoiCategory.PHARMACY)
    if amenity == "fuel":
        result.append(PoiCategory.FUEL_STATION)
    if amenity == "parking":
        result.append(PoiCategory.PARKING)
    if amenity == "charging_station":
        result.append(PoiCategory.EV_CHARGER)
    if tags.get("shop", "").casefold() == "convenience":
        result.append(PoiCategory.CONVENIENCE_STORE)
    if tags.get("harbour", "").casefold() in {"yes", "port"}:
        result.append(PoiCategory.PORT)
    if amenity == "ferry_terminal" or tags.get("building", "").casefold() == "ferry_terminal":
        result.append(PoiCategory.PASSENGER_TERMINAL)
    return list(dict.fromkeys(result))


def _record(osm_type: str, osm_id: int, tags: dict[str, str], lat: float, lng: float, fetched_at: datetime) -> list[PoiRecord]:
    name = tags.get("name") or tags.get("name:ko") or tags.get("name:en")
    if not name:
        return []
    source_id = f"{osm_type}/{osm_id}"
    records: list[PoiRecord] = []
    for category in _categories(tags):
        record_id = hashlib.sha256(f"osm|{source_id}|{category.value}".encode()).hexdigest()[:32]
        records.append(
            PoiRecord(
                id=f"osm:{record_id}",
                source="openstreetmap_local_pbf",
                source_id=source_id,
                name=name,
                category=category,
                lat=lat,
                lng=lng,
                address=tags.get("addr:full") or tags.get("addr:street"),
                metadata={"osm_tags": tags},
                fetched_at=fetched_at,
            )
        )
    return records


def build(pbf_path: Path, database_path: Path) -> int:
    if not pbf_path.is_file():
        raise FileNotFoundError(f"OSM PBF does not exist: {pbf_path}")
    try:
        import osmium
    except ImportError as error:
        raise RuntimeError("osmium is required to build the local POI index") from error

    fetched_at = datetime.now(timezone.utc)
    records: list[PoiRecord] = []

    class Handler(osmium.SimpleHandler):
        def node(self, node) -> None:  # type: ignore[no-untyped-def]
            if not node.location.valid():
                return
            tags = {str(tag.k): str(tag.v) for tag in node.tags}
            records.extend(_record("node", int(node.id), tags, node.location.lat, node.location.lon, fetched_at))

        def way(self, way) -> None:  # type: ignore[no-untyped-def]
            locations = [node.location for node in way.nodes if node.location.valid()]
            if not locations:
                return
            tags = {str(tag.k): str(tag.v) for tag in way.tags}
            lat = sum(location.lat for location in locations) / len(locations)
            lng = sum(location.lon for location in locations) / len(locations)
            records.extend(_record("way", int(way.id), tags, lat, lng, fetched_at))

    Handler().apply_file(str(pbf_path), locations=True)
    return PoiRepository(database_path).replace_records(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=ROUTING_PBF_FILE)
    parser.add_argument("--database", type=Path, default=POI_INDEX_DB)
    args = parser.parse_args()
    count = build(args.pbf, args.database)
    print(f"indexed {count} local POI records into {args.database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
