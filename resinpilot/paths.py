"""Where ResinPilot keeps its files.

Built-in catalogs and images ship inside the package. Everything that belongs to the
user (their printers, print history, previews, settings) lives in a per-user folder:

* Windows: ``%LOCALAPPDATA%\\ResinPilot``
* macOS: ``~/Library/Application Support/ResinPilot``
* Linux: ``~/.local/share/ResinPilot``

Set ``RESINPILOT_HOME`` to use another folder (handy for tests and portable installs).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_data_dir

APP_DIR_NAME = "ResinPilot"


def _package_dir() -> Path:
    # PyInstaller unpacks bundled files under sys._MEIPASS.
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        return Path(frozen) / "resinpilot"
    return Path(__file__).resolve().parent


PACKAGE_DIR = _package_dir()
BUILTIN_DATA_DIR = PACKAGE_DIR / "data"
ASSETS_DIR = PACKAGE_DIR / "assets"
WEB_DIR = PACKAGE_DIR / "web"


def user_dir() -> Path:
    override = os.environ.get("RESINPILOT_HOME", "").strip()
    path = Path(override).expanduser() if override else Path(user_data_dir(APP_DIR_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_path(*parts: str) -> Path:
    """A path inside the user folder; parent folders are created."""
    path = user_dir().joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def preview_dir() -> Path:
    path = user_dir() / "previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ctb_cache_dir() -> Path:
    path = user_dir() / "ctb_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path
