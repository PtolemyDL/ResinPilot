"""``resinpilot`` command: start the web app and open it in the browser."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

from . import __version__
from .branding import APP_NAME, DEFAULT_PORT
from .paths import user_dir


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _open_when_ready(url: str, port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="resinpilot", description=f"{APP_NAME} — resin printer manager")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"web port (default {DEFAULT_PORT})")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="let phones and other computers on your network open the app (listens on all interfaces)",
    )
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    url = f"http://127.0.0.1:{args.port}"
    if not _port_free(host, args.port):
        print(f"Port {args.port} is busy — {APP_NAME} may already be running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 1

    import uvicorn

    from .webapp import SERVER, app, lan_ips

    SERVER.update(port=args.port, lan=args.lan)
    print(f"{APP_NAME} {__version__}")
    print(f"  Open      {url}")
    if args.lan:
        for ip in lan_ips():
            print(f"  Network   http://{ip}:{args.port}")
    print(f"  Data      {user_dir()}")
    print("  Stop with Ctrl+C")
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(url, args.port), daemon=True).start()
    uvicorn.run(app, host=host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
