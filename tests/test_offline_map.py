from pathlib import Path
from urllib.parse import urlparse

from config import BASEMAPS, DEFAULT_BASEMAP_ID
from scripts.verify_offline import check_required_assets, find_external_references


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RETIRED_PREVIEW_FILE = PROJECT_ROOT / "maps" / "tiles" / "korea-basemap.geojson"
RETIRED_STYLE_FILE = PROJECT_ROOT / "static" / "map" / "style.json"
RETIRED_SETUP_SCRIPT = PROJECT_ROOT / "scripts" / "setup_map.py"


def test_application_files_have_no_unapproved_literal_urls() -> None:
    assert find_external_references() == []


def test_required_local_application_assets_exist() -> None:
    assert check_required_assets() == []


def test_liberty_provider_is_configured_in_one_provider_registry() -> None:
    definition = BASEMAPS[DEFAULT_BASEMAP_ID]
    parsed = urlparse(definition["styleUrl"])

    assert DEFAULT_BASEMAP_ID == "liberty"
    assert definition["provider"] == "OpenFreeMap"
    assert parsed.scheme == "https"
    assert parsed.netloc == "tiles.openfreemap.org"
    assert parsed.path == "/styles/liberty"
    assert definition["requestHostnames"] == ["tiles.openfreemap.org"]
    assert "OpenMapTiles" in definition["attribution"]
    assert "OpenStreetMap" in definition["attribution"]


def test_retired_handcrafted_basemap_files_are_not_present() -> None:
    assert not RETIRED_PREVIEW_FILE.exists()
    assert not RETIRED_STYLE_FILE.exists()
    assert not RETIRED_SETUP_SCRIPT.exists()
