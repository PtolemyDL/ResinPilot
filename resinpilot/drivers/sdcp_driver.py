"""SDCP driver — ELEGOO Mars/Saturn (Ultra, 4, 5 …) and other ChiTu-board printers on Wi-Fi/LAN."""

from __future__ import annotations

from typing import Any

from ..sdcp import PrinterLiveStatus, discover_printers, fetch_attributes, fetch_status
from . import FoundPrinter


class SdcpDriver:
    id = "sdcp"
    label = "SDCP (ELEGOO / ChiTu)"

    def discover(self, timeout_s: float = 2.5, target: str = "") -> list[FoundPrinter]:
        found = discover_printers(timeout_s=timeout_s, target=target or "255.255.255.255")
        return [
            FoundPrinter(
                driver=self.id,
                host=p.ip,
                device_id=p.mainboard_id,
                name=p.name,
                model_name=p.machine_name or p.name,
                brand=p.brand,
                firmware=p.firmware_version,
            )
            for p in found
        ]

    def describe(self, printer: FoundPrinter) -> dict[str, Any]:
        return fetch_attributes(printer.host, printer.device_id)

    def status(self, host: str, device_id: str) -> PrinterLiveStatus:
        return fetch_status(host, device_id)
