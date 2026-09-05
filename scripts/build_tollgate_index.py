"""Build the local OSM toll-gate and toll-road SQLite index."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    import osmium
except ModuleNotFoundError as error:  # pragma: no cover - exercised by setup users
    raise SystemExit(
        "PyOsmium is required to build the toll index. "
        "Install dependencies with: python -m pip install -r requirements.txt"
    ) from error

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tolls.names import normalize_toll_name
from backend.tolls.schema import initialize_schema, encode_geometry
from config import ROUTING_PBF_FILE, TOLL_INDEX_DB


# Only current vehicle-road ways are useful for route toll evidence.  The PBF
# also contains toll=yes ferry, footway, construction, and non-highway
# features; indexing those would create false toll matches near a car route.
TOLL_ROAD_HIGHWAYS = frozenset(
    {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
    }
)

def tag_dict(element: object) -> dict[str, str]:
    return {
        str(tag.k): str(tag.v)
        for tag in getattr(element, "tags", ())
    }


def location_pair(node: object) -> tuple[float, float] | None:
    location = getattr(node, "location", None)
    if location is None or not location.valid():
        return None
    return float(location.lon), float(location.lat)


class IndexBuilder(osmium.SimpleHandler):
    """Stream the PBF once and insert only toll-relevant records."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__()
        self.connection = connection
        self.stats: Counter[str] = Counter()
        self.operator_counts: Counter[str] = Counter()

    def _insert_gate(
        self,
        *,
        osm_type: str,
        osm_id: int,
        gate_type: str,
        tags: dict[str, str],
        coordinates: tuple[float, float],
    ) -> None:
        longitude, latitude = coordinates
        name = tags.get("name") or None
        self.connection.execute(
            """
            INSERT OR REPLACE INTO toll_gates(
                id, osm_type, osm_id, name, normalized_name, lat, lng,
                road_name, operator, ref, gate_type, tags_json, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"osm-{osm_type}-{osm_id}",
                osm_type,
                osm_id,
                name,
                normalize_toll_name(name),
                latitude,
                longitude,
                tags.get("road_name") or tags.get("addr:street") or None,
                tags.get("operator"),
                tags.get("ref"),
                gate_type,
                json.dumps(tags, ensure_ascii=False, sort_keys=True),
                "osm",
            ),
        )
        self.stats[f"gate_{gate_type}_{osm_type}"] += 1

    def node(self, node: object) -> None:
        tags = tag_dict(node)
        toll = tags.get("toll")
        if toll == "yes":
            self.stats["toll_yes_nodes"] += 1
        elif toll == "no":
            self.stats["toll_no_nodes"] += 1

        if tags.get("barrier") == "toll_booth":
            gate_type = "toll_booth"
        elif tags.get("highway") == "toll_gantry":
            gate_type = "toll_gantry"
        else:
            return
        coordinates = location_pair(node)
        if coordinates is None:
            self.stats["invalid_gate_locations"] += 1
            return
        self._insert_gate(
            osm_type="node",
            osm_id=int(getattr(node, "id")),
            gate_type=gate_type,
            tags=tags,
            coordinates=coordinates,
        )

    def way(self, way: object) -> None:
        tags = tag_dict(way)
        coordinates = [pair for pair in (location_pair(node) for node in way.nodes) if pair]

        if tags.get("barrier") == "toll_booth" or tags.get("highway") == "toll_gantry":
            gate_type = (
                "toll_booth"
                if tags.get("barrier") == "toll_booth"
                else "toll_gantry"
            )
            self.stats[f"gate_{gate_type}_ways_seen"] += 1
            if coordinates:
                longitude = sum(pair[0] for pair in coordinates) / len(coordinates)
                latitude = sum(pair[1] for pair in coordinates) / len(coordinates)
                self._insert_gate(
                    osm_type="way",
                    osm_id=int(getattr(way, "id")),
                    gate_type=gate_type,
                    tags=tags,
                    coordinates=(longitude, latitude),
                )
            else:
                self.stats["invalid_gate_locations"] += 1

        if tags.get("toll") != "yes" or len(coordinates) < 2:
            return

        if tags.get("highway") not in TOLL_ROAD_HIGHWAYS:
            self.stats["toll_yes_non_vehicle_ways"] += 1
            return

        self.stats["toll_yes_ways"] += 1
        operator = tags.get("operator") or ""
        if operator:
            self.operator_counts[operator] += 1
        longitudes = [pair[0] for pair in coordinates]
        latitudes = [pair[1] for pair in coordinates]
        geometry = encode_geometry(coordinates)
        cursor = self.connection.execute(
            """
            INSERT INTO toll_road_ways(
                osm_id, name, ref, operator, min_lng, max_lng, min_lat, max_lat,
                point_count, geometry, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(getattr(way, "id")),
                tags.get("name"),
                tags.get("ref"),
                tags.get("operator"),
                min(longitudes),
                max(longitudes),
                min(latitudes),
                max(latitudes),
                len(coordinates),
                geometry,
                "osm",
            ),
        )
        road_id = int(cursor.lastrowid)
        self.connection.execute(
            """
            INSERT INTO toll_road_rtree(id, min_lng, max_lng, min_lat, max_lat)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                road_id,
                min(longitudes),
                max(longitudes),
                min(latitudes),
                max(latitudes),
            ),
        )


def build_index(pbf_path: Path, output_path: Path, *, force: bool = False) -> dict[str, object]:
    if not pbf_path.is_file() or pbf_path.stat().st_size == 0:
        raise FileNotFoundError(
            "South Korea map source was not found or is empty.\n"
            f"Expected: {pbf_path}\n"
            "See: maps/README.md"
        )
    if output_path.exists() and not force:
        raise FileExistsError(
            f"Toll index already exists: {output_path}\n"
            "Use --force to rebuild the generated local index."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f"{output_path.name}.building")
    if temporary_path.exists():
        temporary_path.unlink()

    connection = sqlite3.connect(str(temporary_path))
    try:
        initialize_schema(connection)
        connection.execute("PRAGMA journal_mode = MEMORY")
        builder = IndexBuilder(connection)
        print(f"Reading OSM PBF: {pbf_path}")
        builder.apply_file(str(pbf_path), locations=True)
        stats = {
            "schema_version": "1",
            "pbf_file": str(pbf_path.resolve()),
            "pbf_size_bytes": pbf_path.stat().st_size,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "counts": dict(builder.stats),
            "toll_yes_way_operators": dict(builder.operator_counts.most_common(100)),
        }
        for key, value in stats.items():
            if key == "counts" or key == "toll_yes_way_operators":
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        connection.commit()
    finally:
        connection.close()

    temporary_path.replace(output_path)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=ROUTING_PBF_FILE)
    parser.add_argument("--output", type=Path, default=TOLL_INDEX_DB)
    parser.add_argument("--force", action="store_true", help="replace the generated index")
    args = parser.parse_args()
    try:
        result = build_index(args.pbf.resolve(), args.output.resolve(), force=args.force)
    except (FileNotFoundError, FileExistsError, OSError, ValueError, sqlite3.Error) as error:
        print(f"[FAIL] {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[PASS] Toll index written to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
