"""Audit browser files for unapproved network dependencies."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import BASEMAPS, DEFAULT_BASEMAP_ID


APP_OWNED_PATHS = (
    PROJECT_ROOT / "templates",
    PROJECT_ROOT / "static" / "css",
    PROJECT_ROOT / "static" / "js",
    PROJECT_ROOT / "static" / "data",
)
REQUIRED_LOCAL_ASSETS = (
    PROJECT_ROOT / "templates" / "index.html",
    PROJECT_ROOT / "static" / "css" / "style.css",
    PROJECT_ROOT / "static" / "vendor" / "maplibre-gl" / "maplibre-gl.js",
    PROJECT_ROOT / "static" / "vendor" / "maplibre-gl" / "maplibre-gl.css",
    PROJECT_ROOT / "static" / "js" / "map.js",
    PROJECT_ROOT / "static" / "js" / "storage.js",
    PROJECT_ROOT / "static" / "js" / "markers.js",
    PROJECT_ROOT / "static" / "js" / "selection.js",
    PROJECT_ROOT / "static" / "js" / "search.js",
    PROJECT_ROOT / "static" / "js" / "place_layers.js",
    PROJECT_ROOT / "static" / "js" / "route_format.js",
    PROJECT_ROOT / "static" / "js" / "route_layer.js",
    PROJECT_ROOT / "static" / "js" / "route.js",
    PROJECT_ROOT / "static" / "js" / "toll_markers.js",
    PROJECT_ROOT / "static" / "js" / "toll.js",
    PROJECT_ROOT / "static" / "data" / "places.json",
    PROJECT_ROOT / "static" / "favicon.svg",
)
FORBIDDEN_HOSTNAMES = (
    "google",
    "kakao",
    "naver",
    "mapbox",
    "openstreetmap.org",
    "tile.openstreetmap.org",
    "router.project-osrm.org",
    "openrouteservice",
    "graphhopper",
)
FORBIDDEN_STANDALONE_TERMS = ("tmap",)
FORBIDDEN_SERVICE_TERMS = (
    "analytics",
    "telemetry",
    "doubleclick",
    "segment.io",
    "mixpanel",
    "hotjar",
    "plausible.io",
)
URL_PATTERN = re.compile(r"(?P<url>(?:https?:)?//[^\s\"'`<>]+)", re.IGNORECASE)


def allowed_basemap_hostnames() -> set[str]:
    definition = BASEMAPS.get(DEFAULT_BASEMAP_ID, {})
    return {
        str(hostname).lower().rstrip(".")
        for hostname in definition.get("requestHostnames", [])
    }


def iter_app_files() -> list[Path]:
    files: list[Path] = []
    for directory in APP_OWNED_PATHS:
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    return sorted(files)


def find_external_references() -> list[str]:
    """Return literal URLs outside the configured basemap host allowlist."""

    allowed_hosts = allowed_basemap_hostnames()
    violations: list[str] = []
    for path in iter_app_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in URL_PATTERN.finditer(text):
            raw_url = match.group("url")
            parsed = urlparse(
                raw_url if not raw_url.startswith("//") else f"http:{raw_url}"
            )
            hostname = (parsed.hostname or "").lower().rstrip(".")
            if hostname not in allowed_hosts | {"localhost", "127.0.0.1", "::1"}:
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {raw_url}")
        lowered = text.lower()
        for term in (*FORBIDDEN_HOSTNAMES, *FORBIDDEN_SERVICE_TERMS):
            if term in lowered:
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}: forbidden network term '{term}'"
                )
        for term in FORBIDDEN_STANDALONE_TERMS:
            if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}: forbidden network term '{term}'"
                )
    return sorted(set(violations))


def check_basemap_configuration() -> list[str]:
    definition = BASEMAPS.get(DEFAULT_BASEMAP_ID)
    if definition is None:
        return [f"default basemap '{DEFAULT_BASEMAP_ID}' is not configured"]

    style_url = str(definition.get("styleUrl", ""))
    parsed = urlparse(style_url)
    errors = []
    if parsed.scheme != "https":
        errors.append("basemap style URL must use HTTPS")
    if parsed.hostname not in allowed_basemap_hostnames():
        errors.append("basemap style host is not in its request host allowlist")
    if not str(definition.get("attribution", "")).strip():
        errors.append("basemap attribution is missing")
    if not str(definition.get("provider", "")).strip():
        errors.append("basemap provider name is missing")
    return errors


def check_required_assets() -> list[str]:
    return [
        str(path.relative_to(PROJECT_ROOT))
        for path in REQUIRED_LOCAL_ASSETS
        if not path.is_file() or path.stat().st_size == 0
    ]


def main() -> int:
    print("Korea Trip Optimizer basemap/network verification")
    missing_assets = check_required_assets()
    if missing_assets:
        print("[FAIL] Required local application assets are missing:")
        for asset in missing_assets:
            print(f"  - {asset}")
    else:
        print("[PASS] Required local application assets exist.")

    basemap_errors = check_basemap_configuration()
    if basemap_errors:
        print("[FAIL] Basemap provider configuration is invalid:")
        for error in basemap_errors:
            print(f"  - {error}")
    else:
        print(
            f"[PASS] Basemap provider is configured: "
            f"{BASEMAPS[DEFAULT_BASEMAP_ID]['provider']} / {DEFAULT_BASEMAP_ID}."
        )

    violations = find_external_references()
    if violations:
        print("[FAIL] Unapproved external browser references detected:")
        for violation in violations:
            print(f"  - {violation}")
    else:
        print(
            "[PASS] No unapproved external URL, CDN, map provider, "
            "analytics, or telemetry reference in app-owned browser files."
        )

    if missing_assets or basemap_errors or violations:
        return 1
    print("Basemap/network verification passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
