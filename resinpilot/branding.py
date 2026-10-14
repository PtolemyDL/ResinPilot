"""App name and URL helpers."""

from __future__ import annotations

APP_NAME = "ResinPilot"
APP_TAGLINE = "Resin printer manager"
DEFAULT_PORT = 8787
REPO_URL = "https://github.com/PtolemyDL/ResinPilot"


def asset_url(rel: str) -> str:
    """Map catalog photo paths (``assets/…``) to the ``/assets/…`` URL the web app serves."""
    if not rel:
        return ""
    path = str(rel).replace("\\", "/").lstrip("/")
    while path.startswith("assets/assets/"):
        path = path[len("assets/") :]
    if path.startswith("assets/"):
        return f"/{path}"
    return f"/assets/{path}"


def web_urls(ips: list[str], port: int = DEFAULT_PORT) -> list[str]:
    """Local URL first, then one per LAN address."""
    urls = [f"http://127.0.0.1:{port}"]
    for ip in ips:
        url = f"http://{ip}:{port}"
        if url not in urls:
            urls.append(url)
    return urls
