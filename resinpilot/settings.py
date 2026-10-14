"""User preferences, stored as settings.json in the user folder."""

from __future__ import annotations

import json
from typing import Any

from .models import TODAY_LAYER_HEIGHTS
from .paths import user_path

DEFAULTS: dict[str, Any] = {
    # Printer whose vat sensor stands in for room temperature ("" = pick automatically).
    "room_ref_printer_id": "",
    # Used for recommendations when no printer reports a vat temperature.
    "fallback_room_c": 25.0,
    # Never recommend a bottom exposure below this (0 = only each resin's own minimum).
    "min_bottom_exposure_s": 0.0,
    "today_layer_heights": list(TODAY_LAYER_HEIGHTS),
    # Watch Chitubox on this computer for opened models and sent files.
    "watch_chitubox": True,
    # Seconds between printer status refreshes.
    "poll_interval_s": 20.0,
    "theme": "system",
}


def settings_file():
    return user_path("settings.json")


def load_settings() -> dict[str, Any]:
    path = settings_file()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    """Merge ``changes`` into the saved settings. Raises ValueError for values that make no sense."""
    current = load_settings()
    for key, value in changes.items():
        if key in DEFAULTS:
            current[key] = value
    try:
        heights = sorted({round(float(h), 3) for h in current["today_layer_heights"]})
        current["fallback_room_c"] = float(current["fallback_room_c"])
        current["min_bottom_exposure_s"] = float(current["min_bottom_exposure_s"])
        current["poll_interval_s"] = float(current["poll_interval_s"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Not a number: {exc}") from exc
    if not heights or len(heights) > 3 or not all(0.005 <= h <= 0.3 for h in heights):
        raise ValueError("Layer heights: one to three values between 0.005 and 0.3 mm")
    if not 5 <= current["fallback_room_c"] <= 45:
        raise ValueError("Room temperature must be between 5 and 45 °C")
    if not 0 <= current["min_bottom_exposure_s"] <= 120:
        raise ValueError("Minimum bottom exposure must be between 0 and 120 s")
    current["today_layer_heights"] = heights
    current["watch_chitubox"] = bool(current["watch_chitubox"])
    current["poll_interval_s"] = max(5.0, current["poll_interval_s"])
    settings_file().write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current
