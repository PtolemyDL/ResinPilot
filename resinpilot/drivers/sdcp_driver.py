"""SDCP driver — ELEGOO Mars/Saturn (Ultra, 4, 5 …) and other ChiTu-board printers on Wi-Fi/LAN."""

from __future__ import annotations

from typing import Any

from ..sdcp import PrinterLiveStatus, discover_printers, fetch_attributes, fetch_status
from . import FoundPrinter

# SDCP is also spoken by filament (FDM) printers, which ResinPilot leaves alone.
FDM_MODEL_WORDS = ("centauri",)
FDM_FILE_TYPES = {"gcode", "3mf", "bgcode"}


def _is_fdm(model_name: str, file_types: list[str] | None = None) -> bool:
    if any(w in (model_name or "").lower() for w in FDM_MODEL_WORDS):
        return True
    types = {str(t).lower().lstrip(".") for t in (file_types or [])}
    return bool(types) and types <= FDM_FILE_TYPES


class SdcpDriver:
    id = "sdcp"
    label = "SDCP (ELEGOO / ChiTu)"

    def discover(self, timeout_s: float = 2.5, target: str = "") -> list[FoundPrinter]:
        found = discover_printers(timeout_s=timeout_s, target=target or "255.255.255.255")
        out = []
        for p in found:
            printer = FoundPrinter(
                driver=self.id,
                host=p.ip,
                device_id=p.mainboard_id,
                name=p.name,
                model_name=p.machine_name or p.name,
                brand=p.brand,
                firmware=p.firmware_version,
            )
            if not _is_fdm(printer.model_name):
                out.append(printer)
        return out

    def describe(self, printer: FoundPrinter) -> dict[str, Any]:
        return fetch_attributes(printer.host, printer.device_id)

    def is_resin(self, printer: FoundPrinter) -> bool:
        """For models outside the library: ask the printer which files it prints."""
        try:
            details = self.describe(printer)
        except Exception:  # noqa: BLE001 — unknown is treated as resin; the user decides
            return True
        printer.details = details
        return not _is_fdm(details.get("machine_name", ""), details.get("file_types"))

    def status(self, host: str, device_id: str) -> PrinterLiveStatus:
        return fetch_status(host, device_id)
