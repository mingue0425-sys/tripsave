"""Prepare and run TripSave with one fast, repeatable command.

The default path is intentionally lightweight: it reuses or creates the
project virtual environment, installs only when runtime imports are missing,
checks the Playwright browser, initializes the weather cache, verifies the
FastAPI health endpoint, starts Uvicorn, waits for it to become ready, and
opens the local web app in the default browser.

South Korea OSRM data is prepared automatically when it is missing. Existing
PBF/MLD files are reused without repeating the expensive work. The routing
service is started as a managed child process before Uvicorn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import importlib.util
import os
import signal
import subprocess
import sys
import time
import webbrowser
import venv
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


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


def routing_module():
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import setup_routing

    return setup_routing


def prepare_routing(*, engine: str, force_download: bool):
    routing = routing_module()
    try:
        selected_engine, tools = routing.select_engine(engine)
        data_ready = routing.routing_data_ready()
        if force_download or not data_ready:
            print(f"[setup] preparing local OSRM with {selected_engine} engine")
            routing.download_pbf(force=force_download)
            routing.preprocess(selected_engine, tools)
        else:
            print("[pass] local OSRM PBF/MLD data already ready")
        return routing, selected_engine
    except routing.SetupError as error:
        raise QuickstartError(str(error)) from error


def start_routing_service(routing, *, engine: str) -> subprocess.Popen[bytes] | None:
    if routing.local_osrm_ready():
        print("[pass] local OSRM service already ready on 127.0.0.1:5000")
        return None

    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "setup_routing.py"),
        "--start",
        "--engine",
        engine,
    ]
    print("[run] " + " ".join(command))
    popen_kwargs: dict[str, object] = {
        "cwd": PROJECT_ROOT,
        "env": os.environ.copy(),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, **popen_kwargs)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if routing.local_osrm_ready():
            print("[pass] local OSRM service ready on 127.0.0.1:5000")
            return process
        if process.poll() is not None:
            raise QuickstartError(
                f"local OSRM startup failed with exit code {process.returncode}"
            )
        time.sleep(0.5)

    stop_routing_service(process)
    raise QuickstartError("local OSRM did not become ready within 45 seconds")


def stop_routing_service(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def verify_fastapi(*, expect_routing: bool) -> None:
    os.environ.setdefault("KTO_TOLL_BROWSER_WARMUP", "0")
    sys.path.insert(0, str(PROJECT_ROOT))
    from fastapi.testclient import TestClient

    import app as application

    with TestClient(application.app) as client:
        response = client.get("/health")
    if response.status_code != 200 or response.json().get("status") != "ok":
        raise QuickstartError(f"FastAPI health check failed: {response.status_code}")
    print(f"[pass] FastAPI health: {response.json()}")
    if expect_routing:
        routing_response = client.get("/api/routing/status")
        if routing_response.status_code != 200:
            raise QuickstartError(
                f"local OSRM health check failed: {routing_response.status_code}"
            )
        print(f"[pass] local OSRM API: {routing_response.json()}")


def local_app_host(host: str) -> str:
    """Return a loopback host suitable for local health checks and browsers."""
    return "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host


def app_url(*, host: str, port: int) -> str:
    browser_host = local_app_host(host)
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}"


def app_health_ready(url: str) -> bool:
    try:
        with urlopen(f"{url}/health", timeout=1) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return False
    return payload.get("status") == "ok"


def wait_for_app(process: subprocess.Popen[bytes], url: str, *, timeout_s: float = 45) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise QuickstartError(
                f"TripSave server exited during startup with code {process.returncode}"
            )
        if app_health_ready(url):
            return
        time.sleep(0.25)
    raise QuickstartError(f"TripSave did not become ready within {timeout_s:g} seconds")


def stop_app_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_server(*, host: str, port: int, reload: bool, open_browser: bool) -> int:
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
    process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=environment)
    url = app_url(host=host, port=port)
    try:
        wait_for_app(process, url)
        print(f"[ready] TripSave: {url}")
        if open_browser:
            try:
                opened = webbrowser.open(url, new=2)
            except Exception as error:  # noqa: BLE001 - browser is optional
                print(f"[info] Could not open the browser automatically: {error}")
            else:
                message = "[open] Browser launched" if opened else "[info] Open this URL in a browser"
                print(f"{message}: {url}")
        return process.wait()
    except KeyboardInterrupt:
        print("\n[stop] TripSave stopped")
        return 130
    finally:
        stop_app_process(process)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="prepare and verify only; do not start Uvicorn")
    parser.add_argument("--no-install", action="store_true", help="fail instead of installing missing packages/browser")
    parser.add_argument("--no-browser", action="store_true", help="skip the Chromium check and disable browser fallback")
    parser.add_argument("--skip-routing", action="store_true", help="skip OSRM data/service preparation")
    parser.add_argument(
        "--routing-engine",
        choices=("auto", "native", "docker"),
        default="auto",
        help="OSRM runtime to use when preparing missing data (default: auto)",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="replace the existing PBF, verify it, and rebuild the MLD graph",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Uvicorn bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Uvicorn port (default: 8765)")
    parser.add_argument("--reload", action="store_true", help="enable Uvicorn auto-reload")
    parser.add_argument("--no-open", action="store_true", help="do not open the app in the default browser")
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
    routing_process = None
    try:
        if args.skip_routing:
            print("[info] OSRM preparation skipped")
        else:
            routing, selected_engine = prepare_routing(
                engine=args.routing_engine,
                force_download=args.force_download,
            )
            routing_process = start_routing_service(routing, engine=selected_engine)
        initialize_weather_cache()
        verify_fastapi(expect_routing=not args.skip_routing)

        if args.check:
            print("[ready] TripSave is prepared. Start with: python start.py")
            return 0
        return run_server(
            host=args.host,
            port=args.port,
            reload=args.reload,
            open_browser=not args.no_open,
        )
    finally:
        stop_routing_service(routing_process)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[stop] TripSave stopped")
        raise SystemExit(130)
    except QuickstartError as error:
        print(f"[fail] {error}", file=sys.stderr)
        raise SystemExit(1) from error
