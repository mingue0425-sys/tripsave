"""Prepare and run the local South Korea OSRM car engine."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import site
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    OSM_PBF_CHECKSUM_URL,
    OSM_PBF_URL,
    OSRM_BIND_HOST,
    OSRM_DOCKER_IMAGE,
    OSRM_PORT,
    OSRM_PROFILE,
    ROUTING_DATA_BASE,
    ROUTING_PBF_FILE,
)


OSRM_TOOL_NAMES = ("osrm-extract", "osrm-partition", "osrm-customize", "osrm-routed")
REQUIRED_MLD_SUFFIXES = (
    ".osrm.properties",
    ".osrm.cells",
    ".osrm.partition",
    ".osrm.mldgr",
    ".osrm.cell_metrics",
)


class SetupError(RuntimeError):
    """A user-actionable routing setup failure."""


def _native_bin_candidates() -> list[Path]:
    candidates: list[Path] = []
    package_spec = importlib.util.find_spec("osrm")
    if package_spec and package_spec.origin:
        candidates.append(Path(package_spec.origin).parent.parent / "bin")
    for site_path in (*site.getsitepackages(), site.getusersitepackages()):
        candidates.append(Path(site_path) / "bin")
    candidates.append(Path(sys.prefix) / "bin")
    return candidates


def find_osrm_tools() -> dict[str, Path]:
    """Find a complete native OSRM toolchain, if installed."""

    extension = ".exe" if os.name == "nt" else ""
    tools: dict[str, Path] = {}
    for name in OSRM_TOOL_NAMES:
        executable = shutil.which(name)
        if executable:
            tools[name] = Path(executable)
            continue
        for directory in _native_bin_candidates():
            candidate = directory / f"{name}{extension}"
            if candidate.is_file():
                tools[name] = candidate
                break
    return tools


def find_car_profile() -> Path | None:
    package_spec = importlib.util.find_spec("osrm")
    candidates: list[Path] = []
    if package_spec and package_spec.origin:
        candidates.append(
            Path(package_spec.origin).parent.parent
            / "share"
            / "osrm"
            / "profiles"
            / "car.lua"
        )
    for site_path in (*site.getsitepackages(), site.getusersitepackages()):
        candidates.append(Path(site_path) / "share" / "osrm" / "profiles" / "car.lua")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def docker_is_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        result = subprocess.run(
            [docker, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def select_engine(requested: str) -> tuple[str, dict[str, Path]]:
    native_tools = find_osrm_tools()
    native_complete = all(name in native_tools for name in OSRM_TOOL_NAMES)
    if requested in {"auto", "native"} and native_complete:
        return "native", native_tools
    if requested == "native":
        missing = [name for name in OSRM_TOOL_NAMES if name not in native_tools]
        raise SetupError(
            "Native OSRM tools are incomplete. Missing: "
            + ", ".join(missing)
            + ". Install osrm-bindings on Python 3.12+ or use Docker Desktop."
        )
    if requested in {"auto", "docker"} and docker_is_available():
        return "docker", {}
    if requested == "docker":
        raise SetupError(
            "Docker Desktop is not available. Start Docker or choose the native OSRM toolchain."
        )
    raise SetupError(
        "No local OSRM runtime was found. Install osrm-bindings on Python 3.12+, "
        "or install Docker Desktop. A public OSRM server is never used as fallback."
    )


def _http_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "KoreaTripOptimizer/0.3 setup"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (OSError, URLError) as error:
        raise SetupError(f"Could not download setup metadata from {url}: {error}") from error


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_md5() -> str:
    checksum_text = _http_text(OSM_PBF_CHECKSUM_URL)
    match = re.search(r"\b([0-9a-fA-F]{32})\b", checksum_text)
    if not match:
        raise SetupError(f"No MD5 checksum was found at {OSM_PBF_CHECKSUM_URL}.")
    return match.group(1).lower()


def verify_pbf_checksum(path: Path) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        raise SetupError(f"South Korea OSM PBF is missing or empty: {path}")
    actual = _md5(path)
    expected = expected_md5()
    if actual != expected:
        raise SetupError(
            f"PBF checksum mismatch for {path.name}: expected {expected}, got {actual}."
        )
    return actual


def download_pbf(force: bool = False) -> None:
    ROUTING_PBF_FILE.parent.mkdir(parents=True, exist_ok=True)
    if ROUTING_PBF_FILE.exists() and not force:
        actual = verify_pbf_checksum(ROUTING_PBF_FILE)
        print(f"PBF already exists and passed MD5 verification: {ROUTING_PBF_FILE}")
        print(f"MD5 {actual}. Use --force-download to intentionally refresh the extract.")
        return

    temporary_file = ROUTING_PBF_FILE.with_name(
        f"{ROUTING_PBF_FILE.name}.download"
    )
    if temporary_file.exists():
        temporary_file.unlink()
    request = Request(OSM_PBF_URL, headers={"User-Agent": "KoreaTripOptimizer/0.3 setup"})
    print(f"Downloading {OSM_PBF_URL}")
    try:
        with urlopen(request, timeout=60) as response, temporary_file.open("wb") as target:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
        actual = _md5(temporary_file)
        expected = expected_md5()
        if actual != expected:
            raise SetupError(
                f"PBF checksum mismatch: expected {expected}, got {actual}."
            )
        temporary_file.replace(ROUTING_PBF_FILE)
        print(
            f"Downloaded {ROUTING_PBF_FILE} ({ROUTING_PBF_FILE.stat().st_size:,} bytes); "
            f"MD5 {actual}."
        )
    except SetupError:
        raise
    except (OSError, URLError) as error:
        raise SetupError(f"Could not download the South Korea OSM PBF: {error}") from error
    finally:
        if temporary_file.exists():
            temporary_file.unlink()


def routing_data_ready() -> bool:
    return all(
        Path(f"{ROUTING_DATA_BASE}{suffix}").is_file()
        and Path(f"{ROUTING_DATA_BASE}{suffix}").stat().st_size > 0
        for suffix in REQUIRED_MLD_SUFFIXES
    )


def _run(command: list[str], environment: dict[str, str] | None = None) -> None:
    print("$ " + " ".join(command))
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode != 0:
        raise SetupError(f"OSRM preprocessing command failed with exit code {result.returncode}.")


def native_environment() -> dict[str, str]:
    environment = os.environ.copy()
    package_spec = importlib.util.find_spec("osrm")
    if package_spec and package_spec.origin:
        dll_directory = Path(package_spec.origin).parent.parent / "osrm_bindings.libs"
        if dll_directory.is_dir():
            environment["PATH"] = str(dll_directory) + os.pathsep + environment.get(
                "PATH", ""
            )
    return environment


def docker_command(*arguments: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--mount",
        f"type=bind,source={PROJECT_ROOT},target=/data",
        OSRM_DOCKER_IMAGE,
        *arguments,
    ]


def preprocess(engine: str, tools: dict[str, Path]) -> None:
    if not ROUTING_PBF_FILE.is_file() or ROUTING_PBF_FILE.stat().st_size == 0:
        raise SetupError(
            "South Korea OSM PBF was not found.\n"
            f"Expected: {ROUTING_PBF_FILE}\n"
            "Run: python scripts/setup_routing.py --download"
        )
    ROUTING_DATA_BASE.parent.mkdir(parents=True, exist_ok=True)
    output_base = Path(f"{ROUTING_DATA_BASE}.osrm")
    if engine == "native":
        environment = native_environment()
        profile = find_car_profile()
        if profile is None:
            raise SetupError(
                "The native OSRM binaries were found, but the official car.lua profile was not."
            )
        _run(
            [
                str(tools["osrm-extract"]),
                "-p",
                str(profile),
                "-o",
                str(output_base),
                str(ROUTING_PBF_FILE),
            ],
            environment,
        )
        _run([str(tools["osrm-partition"]), str(ROUTING_DATA_BASE)], environment)
        _run([str(tools["osrm-customize"]), str(ROUTING_DATA_BASE)], environment)
    else:
        pbf_in_container = "/data/maps/source/south-korea-latest.osm.pbf"
        output_in_container = "/data/maps/osrm/south-korea-latest.osrm"
        base_in_container = "/data/maps/osrm/south-korea-latest"
        _run(
            docker_command(
                "osrm-extract",
                "-p",
                "/opt/car.lua",
                "-o",
                output_in_container,
                pbf_in_container,
            )
        )
        _run(docker_command("osrm-partition", base_in_container))
        _run(docker_command("osrm-customize", base_in_container))
    if not routing_data_ready():
        raise SetupError("OSRM preprocessing finished without all required MLD files.")
    print("Local OSRM MLD dataset is ready.")


def _probe_url() -> str:
    return (
        f"http://{OSRM_BIND_HOST}:{OSRM_PORT}/nearest/v1/{OSRM_PROFILE}/"
        "126.978,37.5665?number=1"
    )


def local_osrm_ready() -> bool:
    try:
        with urlopen(_probe_url(), timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return False
    return response.status == 200 and payload.get("code") == "Ok"


def print_status() -> bool:
    pbf_ready = ROUTING_PBF_FILE.is_file() and ROUTING_PBF_FILE.stat().st_size > 0
    data_ready = routing_data_ready()
    tools = find_osrm_tools()
    native_ready = all(name in tools for name in OSRM_TOOL_NAMES)
    docker_ready = docker_is_available()
    service_ready = local_osrm_ready()
    print("Korea Trip Optimizer local routing setup")
    print(f"[{'PASS' if pbf_ready else 'FAIL'}] PBF: {ROUTING_PBF_FILE}")
    print(f"[{'PASS' if data_ready else 'FAIL'}] MLD data base: {ROUTING_DATA_BASE}")
    print(f"[{'PASS' if native_ready else 'INFO'}] Native OSRM tools: {native_ready}")
    print(f"[{'PASS' if docker_ready else 'INFO'}] Docker runtime: {docker_ready}")
    print(
        f"[{'PASS' if service_ready else 'FAIL'}] Local OSRM service: "
        f"http://{OSRM_BIND_HOST}:{OSRM_PORT}"
    )
    if not pbf_ready:
        print(f"Next: python scripts/setup_routing.py --download")
    if pbf_ready and not data_ready:
        print("Next: python scripts/setup_routing.py --preprocess")
    if data_ready and not service_ready:
        print("Next: python scripts/setup_routing.py --start")
    return pbf_ready and data_ready and service_ready


def start_service(engine: str, tools: dict[str, Path]) -> None:
    if not routing_data_ready():
        raise SetupError(
            "Routing data is not ready. Run: python scripts/setup_routing.py --preprocess"
        )
    if local_osrm_ready():
        print(f"Local OSRM is already ready on port {OSRM_PORT}.")
        return

    if engine == "native":
        command = [
            str(tools["osrm-routed"]),
            "--algorithm",
            "mld",
            "--ip",
            OSRM_BIND_HOST,
            "--port",
            str(OSRM_PORT),
            str(ROUTING_DATA_BASE),
        ]
        environment = native_environment()
    else:
        command = docker_command(
            "osrm-routed",
            "--algorithm",
            "mld",
            "--ip",
            "0.0.0.0",
            "--port",
            str(OSRM_PORT),
            "/data/maps/osrm/south-korea-latest",
        )
        command[2:2] = ["--name", "kto-osrm", "-p", f"127.0.0.1:{OSRM_PORT}:5000"]
        environment = None

    print("$ " + " ".join(command))
    process = subprocess.Popen(command, env=environment)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SetupError(
                    f"Local OSRM exited during startup with code {process.returncode}."
                )
            if local_osrm_ready():
                print(f"Local OSRM is ready on http://{OSRM_BIND_HOST}:{OSRM_PORT}.")
                # Poll instead of blocking forever so Ctrl+C reaches this
                # parent process even when Windows keeps the child alive
                # during its graceful shutdown.
                while True:
                    try:
                        process.wait(timeout=1)
                        return
                    except subprocess.TimeoutExpired:
                        continue
            time.sleep(0.5)
        raise SetupError("Local OSRM did not become ready within 30 seconds.")
    except KeyboardInterrupt:
        print("Stopping local OSRM…")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print("Local OSRM did not stop gracefully; terminating it.")
                process.kill()
                process.wait(timeout=5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="download the current Geofabrik PBF")
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="replace the existing PBF after checksum verification",
    )
    parser.add_argument("--preprocess", action="store_true", help="build the OSRM MLD dataset")
    parser.add_argument("--start", action="store_true", help="run local osrm-routed in the foreground")
    parser.add_argument("--check", action="store_true", help="check local files and service health")
    parser.add_argument(
        "--engine",
        choices=("auto", "native", "docker"),
        default="auto",
        help="select native OSRM binaries or Docker (default: auto)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.force_download:
            args.download = True
        if args.download:
            download_pbf(force=args.force_download)
        engine = None
        tools: dict[str, Path] = {}
        if args.preprocess or args.start:
            engine, tools = select_engine(args.engine)
        if args.preprocess:
            preprocess(engine, tools)
        if args.start:
            start_service(engine, tools)
        if args.check or not (args.download or args.preprocess or args.start):
            return 0 if print_status() else 1
        return 0
    except SetupError as error:
        print(f"[FAIL] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
