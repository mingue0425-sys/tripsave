"""Inspect toll-related OSM tags in the local South Korea PBF.

This is deliberately a read-only, streaming inspection.  It does not build a
routing graph or infer toll prices; its purpose is to make the tag assumptions
used by the toll index observable against the actual extract in this project.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import osmium


INTERESTING_KEYS = (
    "barrier",
    "highway",
    "toll",
    "operator",
    "name",
    "ref",
)


class TollTagHandler(osmium.SimpleHandler):
    """Count relevant tags and retain a small candidate sample."""

    def __init__(self) -> None:
        super().__init__()
        self.element_counts: collections.Counter[str] = collections.Counter()
        self.value_counts: dict[str, collections.Counter[str]] = {
            key: collections.Counter() for key in INTERESTING_KEYS
        }
        self.candidate_counts: collections.Counter[str] = collections.Counter()
        self.toll_way_values: dict[str, collections.Counter[str]] = {
            key: collections.Counter() for key in ("operator", "name", "ref")
        }
        self.samples: list[dict[str, object]] = []

    def _inspect(self, element: object, element_type: str) -> None:
        tags = getattr(element, "tags", {})
        if not tags:
            return
        self.element_counts[element_type] += 1
        for key in INTERESTING_KEYS:
            value = tags.get(key)
            if value is not None:
                self.value_counts[key][str(value)] += 1

        barrier = tags.get("barrier")
        highway = tags.get("highway")
        toll = tags.get("toll")
        if element_type == "way" and toll == "yes":
            for key, counter in self.toll_way_values.items():
                value = tags.get(key)
                if value is not None:
                    counter[str(value)] += 1
        candidate_kind: str | None = None
        if barrier == "toll_booth":
            candidate_kind = "barrier=toll_booth"
        elif highway == "toll_gantry":
            candidate_kind = "highway=toll_gantry"
        elif toll is not None:
            candidate_kind = f"toll={toll}"
        if candidate_kind is None:
            return

        self.candidate_counts[candidate_kind] += 1
        if len(self.samples) >= 50:
            return
        sample: dict[str, object] = {
            "type": element_type,
            "id": int(getattr(element, "id", 0)),
            "kind": candidate_kind,
            "tags": {key: str(tags[key]) for key in INTERESTING_KEYS if key in tags},
        }
        if element_type == "node":
            location = getattr(element, "location", None)
            if location is not None and location.valid():
                sample["lng"] = float(location.lon)
                sample["lat"] = float(location.lat)
        self.samples.append(sample)

    def node(self, node: object) -> None:
        self._inspect(node, "node")

    def way(self, way: object) -> None:
        self._inspect(way, "way")

    def relation(self, relation: object) -> None:
        self._inspect(relation, "relation")


def inspect(path: Path) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(
            "South Korea map source was not found or is empty.\n"
            f"Expected: {path}"
        )
    handler = TollTagHandler()
    handler.apply_file(str(path), locations=True)
    return {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "element_counts_with_interesting_tags": dict(handler.element_counts),
        "tag_values_top_100": {
            key: {
                "distinct": len(counter),
                "top": dict(counter.most_common(100)),
            }
            for key, counter in handler.value_counts.items()
            if counter
        },
        "toll_candidate_counts": dict(handler.candidate_counts),
        "toll_yes_way_values_top_100": {
            key: {
                "distinct": len(counter),
                "top": dict(counter.most_common(100)),
            }
            for key, counter in handler.toll_way_values.items()
            if counter
        },
        "candidate_sample": handler.samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pbf", type=Path, help="South Korea .osm.pbf path")
    args = parser.parse_args()
    try:
        result = inspect(args.pbf.resolve())
    except (FileNotFoundError, OSError, osmium.InvalidLocationError) as error:
        print(f"[FAIL] {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
