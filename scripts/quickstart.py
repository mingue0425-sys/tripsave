"""Prepare and run TripSave with one fast, repeatable command.

The default path is intentionally lightweight: it reuses or creates the
project virtual environment, installs only when runtime imports are missing,
checks the Playwright browser, initializes the weather cache, verifies the
FastAPI health endpoint, and then starts Uvicorn.

South Korea OSRM data is intentionally not downloaded or preprocessed here.
That dataset is large and remains an explicit opt-in operation through
``scripts/setup_routing.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import subprocess
import sys
import venv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = Path(__file__).resolve()
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
REQUIRED_IMPORTS = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
    "httpx": "httpx",
    "bs4": "beautifulsoup4",
    "playwright": "playwright",
    "pytest": "pytest",
    "osmium": "osmium",
    "osrm": "osrm-bindings",
}


class QuickstartError(RuntimeError):
    """A user-actionable quickstart failure."""


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def is_project_python() -> bool:
    executable = Path(sys.executable).absolute()
    expected = venv_python().absolute()
    # ``venv`` may symlink its interpreter to the system installation.  Do
    # not resolve symlinks here: the path inside .venv is the important part.
    return executable == expected or VENV_DIR in executable.parents


def reexec_in_project_venv(argv: list[str]) -> None:
    """Use the project interpreter even when called with system Python."""

    if is_project_python():
        return
    if os.environ.get("TRIPSAVE_QUICKSTART_REEXEC") == "1":
        raise QuickstartError(
            "quickstart could not switch to the project virtual environment: "
            f"{venv_python()}"
        )
    if not venv_python().exists():
        print(f"[setup] creating virtual environment: {VENV_DIR}")
        builder = venv.EnvBuilder(with_pip=True, clear=False, symlinks=True)
        builder.create(VENV_DIR)
    environment = os.environ.copy()
    environment["TRIPSAVE_QUICKSTART_REEXEC"] = "1"
    os.execve(
        str(venv_python()),
        [str(venv_python()), str(SCRIPT_PATH), *argv],
        environment,
    )


def missing_imports() -> list[str]:
    return [
        distribution
        for module, distribution in REQUIRED_IMPORTS.items()
        if importlib.util.find_spec(module) is None
    ]


def install_runtime_dependencies(*, allow_install: bool) -> None:
    missing = missing_imports()
    if not missing:
        print("[pass] runtime dependencies already installed")
        return
    if not allow_install:
        raise QuickstartError(
            "missing runtime dependencies: "
            + ", ".join(missing)
            + "\nRun without --no-install to install them."
        )
    print("[setup] installing missing runtime dependencies: " + ", ".join(missing))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(REQUIREMENTS_FILE),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise QuickstartError("pip install failed")


async def browser_launch_check() -> bool:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await browser.close()
    return True


def ensure_playwright_browser(*, allow_install: bool) -> None:
    try:
        asyncio.run(browser_launch_check())
        print("[pass] Playwright Chromium is ready")
        return
    except Exception as error:  # noqa: BLE001 - converted to setup guidance
        if not allow_install:
            raise QuickstartError(
                "Playwright Chromium is unavailable. "
                "Run without --no-install to install it."
            ) from error

    print("[setup] installing Playwright Chromium")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise QuickstartError("Playwright Chromium installation failed")
    try:
        asyncio.run(browser_launch_check())
    except Exception as error:  # noqa: BLE001 - final actionable failure
        raise QuickstartError("Playwright Chromium is still unavailable after installation") from error
    print("[pass] Playwright Chromium installed")


def initialize_weather_cache() -> Path:
    sys.path.insert(0, str(PROJECT_ROOT))
    from backend.weather.repository import WeatherCache
    from config import WEATHER_CACHE_DB, WEATHER_CACHE_TTL_S, WEATHER_STALE_MAX_AGE_S

    cache = WeatherCache(
        WEATHER_CACHE_DB,
        ttl_s=WEATHER_CACHE_TTL_S,
        stale_max_age_s=WEATHER_STALE_MAX_AGE_S,
    )
    cache.close()
    print(f"[pass] weather cache ready: {WEATHER_CACHE_DB}")
    return WEATHER_CACHE_DB


def verify_fastapi() -> None:
    os.environ.setdefault("KTO_TOLL_BROWSER_WARMUP", "0")
    sys.path.insert(0, str(PROJECT_ROOT))
    from fastapi.testclient import TestClient

    import app as application

    with TestClient(application.app) as client:
        response = client.get("/health")
    if response.status_code != 200 or response.json().get("status") != "ok":
        raise QuickstartError(f"FastAPI health check failed: {response.status_code}")
    print(f"[pass] FastAPI health: {response.json()}")


def run_server(*, host: str, port: int, reload: bool) -> int:
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if reload:
        command.append("--reload")
    print("[run] " + " ".join(command))
    environment = os.environ.copy()
    environment.setdefault("KTO_TOLL_BROWSER_WARMUP", "0")
    return subprocess.run(command, cwd=PROJECT_ROOT, env=environment, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="prepare and verify only; do not start Uvicorn")
    parser.add_argument("--no-install", action="store_true", help="fail instead of installing missing packages/browser")
    parser.add_argument("--no-browser", action="store_true", help="skip the Chromium check and disable browser fallback")
    parser.add_argument("--host", default="127.0.0.1", help="Uvicorn bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Uvicorn port (default: 8765)")
    parser.add_argument("--reload", action="store_true", help="enable Uvicorn auto-reload")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise QuickstartError("--port must be between 1 and 65535")
    if sys.version_info < (3, 11):
        raise QuickstartError("Python 3.11 or newer is required")

    reexec_in_project_venv(sys.argv[1:] if argv is None else argv)
    install_runtime_dependencies(allow_install=not args.no_install)
    if args.no_browser:
        os.environ["KTO_WEATHER_KMA_WEB_FALLBACK_ENABLED"] = "0"
        print("[info] Playwright check skipped; weather browser fallback disabled")
    else:
        ensure_playwright_browser(allow_install=not args.no_install)
    initialize_weather_cache()
    verify_fastapi()

    if args.check:
        print("[ready] TripSave is prepared. Start with: python scripts/quickstart.py")
        return 0
    return run_server(host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[stop] TripSave stopped")
        raise SystemExit(130)
    except QuickstartError as error:
        print(f"[fail] {error}", file=sys.stderr)
        raise SystemExit(1) from error
