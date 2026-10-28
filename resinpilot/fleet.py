"""The user's own printers (fleet.yaml in the user folder).

Each entry points at a printer model from the library. Printers whose model is not in the
library carry their own description (resolution, build volume) as reported on discovery.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

from .paths import user_path


@dataclass
class FleetEntry:
    id: str
    name: str
    model_id: str
    driver: str = "sdcp"
    host: str = ""
    device_id: str = ""
    photo: str = ""
    # Only for models missing from the library: what the printer reported about itself.
    model: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        return {k: v for k, v in out.items() if v not in ("", {}, None)}


def fleet_file():
    return user_path("fleet.yaml")


def printer_key(driver: str, device_id: str = "", host: str = "") -> str:
    """Stable identity for one physical printer: mainboard id, or its address when that is all we have."""
    ident = (device_id or "").strip() or (host or "").strip()
    return f"{(driver or 'sdcp').strip()}:{ident}"


def _load_raw() -> dict[str, Any]:
    path = fleet_file()
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _entry_from(item: dict[str, Any]) -> FleetEntry | None:
    if not item.get("id") or not item.get("model_id"):
        return None
    return FleetEntry(
        id=str(item["id"]),
        name=str(item.get("name") or item["id"]),
        model_id=str(item["model_id"]),
        driver=str(item.get("driver") or "sdcp"),
        host=str(item.get("host") or ""),
        device_id=str(item.get("device_id") or ""),
        photo=str(item.get("photo") or ""),
        model=dict(item.get("model") or {}),
    )


def load_fleet() -> list[FleetEntry]:
    out: list[FleetEntry] = []
    for item in _load_raw().get("printers") or []:
        entry = _entry_from(item)
        if entry is not None:
            out.append(entry)
    return out


def load_retired() -> list[FleetEntry]:
    """Printers the user removed. Kept so adding the same machine again uses the same id and history."""
    out: list[FleetEntry] = []
    for item in _load_raw().get("retired") or []:
        entry = _entry_from(item)
        if entry is not None:
            out.append(entry)
    return out


def save_fleet(entries: list[FleetEntry], retired: list[FleetEntry] | None = None) -> None:
    if retired is None:
        retired = load_retired()
    body = {
        "printers": [e.to_dict() for e in entries],
        "retired": [e.to_dict() for e in retired],
    }
    fleet_file().write_text(
        "# Your printers. Edit freely; ResinPilot rewrites this file when you add or remove printers.\n"
        "# 'retired' keeps the id of a printer you removed, so its history comes back if you add it again.\n"
        + yaml.safe_dump(body, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "printer"


def unique_id(base: str, taken: set[str]) -> str:
    candidate = slug(base)
    n = 2
    while candidate in taken:
        candidate = f"{slug(base)}_{n}"
        n += 1
    return candidate
