"""``resinpilot`` command: start the app and open it in its own window."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .branding import APP_NAME, DEFAULT_PORT
from .paths import user_dir

PORT_TRIES = 20


def _port_free(host: str, port: int) -> bool:
    # Windows lets 127.0.0.1:port bind next to a server already on 0.0.0.0:port, so ask first.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return False
    except OSError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _resinpilot_at(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=2) as res:
            return json.loads(res.read()).get("app_name") == APP_NAME
    except (OSError, ValueError):
        return False


def _app_browser() -> str | None:
    """A Chromium browser that can open a page as a standalone app window."""
    candidates = [shutil.which(n) for n in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser")]
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", ""), os.environ.get("LOCALAPPDATA", "")):
            candidates += [
                str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    return next((c for c in candidates if c and Path(c).is_file()), None)


def open_ui(url: str, app_window: bool = True) -> None:
    browser = _app_browser() if app_window else None
    if browser:
        profile = user_dir() / "window"
        try:
            subprocess.Popen(
                [browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1500,950", "--no-first-run"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    webbrowser.open(url)


def _open_when_ready(url: str, port: int, app_window: bool) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    open_ui(url, app_window)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="resinpilot", description=f"{APP_NAME} — resin printer manager")
    parser.add_argument("--port", type=int, default=None, help=f"web port (default {DEFAULT_PORT}, or the next free one)")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="let phones and other computers on your network open the app (listens on all interfaces)",
    )
    parser.add_argument("--no-browser", action="store_true", help="don't open a window")
    parser.add_argument("--tab", action="store_true", help="open in a normal browser tab instead of an app window")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    app_window = not args.tab
    ports = [args.port] if args.port else range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES)
    port = None
    for candidate in ports:
        if _port_free(host, candidate):
            port = candidate
            break
        if _resinpilot_at(candidate):
            print(f"{APP_NAME} is already running at http://127.0.0.1:{candidate}")
            if not args.no_browser:
                open_ui(f"http://127.0.0.1:{candidate}", app_window)
            return 0
    if port is None:
        print(f"Port {args.port} is used by another program. Try --port with another number.")
        return 1

    import uvicorn

    from .webapp import SERVER, app, lan_ips

    url = f"http://127.0.0.1:{port}"
    SERVER.update(port=port, lan=args.lan)
    print(f"{APP_NAME} {__version__}")
    print(f"  Open      {url}")
    if args.lan:
        for ip in lan_ips():
            print(f"  Network   http://{ip}:{port}")
    print(f"  Data      {user_dir()}")
    print("  Stop with Ctrl+C or close this window")
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(url, port, app_window), daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
